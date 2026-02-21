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
# CONFIGURAÇÕES v70.0 - FOCO EM VALOR, PILATES & FAQ
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

def simular_digitacao(to):
    time.sleep(0.8)

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

        # --- FAQ (FAC) AUTOMÁTICO ---
        perguntas_frequentes = {
            "estacionamento": "Temos estacionamento conveniado logo em frente à unidade, garantindo total segurança e conforto para você! 🚗",
            "localização": f"Nossa unidade {unit} fica em uma localização privilegiada e de fácil acesso. Deseja que eu envie a localização exata pelo Google Maps?",
            "horário": "Funcionamos de segunda a sexta, das 07h às 21h, e aos sábados das 08h às 12h. ⏰"
        }
        for chave, resposta in perguntas_frequentes.items():
            if chave in msg_recebida.lower():
                enviar_texto(phone, resposta)
                return jsonify({"status": "success"}), 200

        # --- COMANDO DE RESET ---
        if msg_recebida.lower() in ["resetar tudo", "recomeçar"]:
            requests.post(WIX_URL, json={"from": phone, "status": "triagem"})
            enviar_texto(phone, "🔄 Atendimento reiniciado! Como podemos ajudar?")
            return jsonify({"status": "success"}), 200

        # 1. CONSULTA AO WIX
        res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida, "unit": unit}, timeout=15)
        info = res_wix.json()
        
        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        is_veteran = info.get("isVeteran", False)

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
                enviar_botoes(phone, "Para passarmos as informações corretas, como você pretende realizar as aulas?", ["Wellhub / Totalpass", "Saúde Caixa", "Plano Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "pilates_triagem_modalidade"})
            elif msg_recebida in ["Recovery", "Liberação Miofascial"]:
                enviar_texto(phone, f"O serviço de **{msg_recebida}** é focado em performance e bem-estar, realizado exclusivamente de forma **PARTICULAR**. ✨")
                enviar_texto(phone, "Para darmos sequência e passarmos os horários, por favor digite seu **NOME COMPLETO**:")
                requests.post(WIX_URL, json={"from": phone, "status": "performance_nome", "modalidade": "particular"})
            else:
                enviar_botoes(phone, "Deseja atendimento pelo CONVÊNIO ou PARTICULAR?", ["Convênio", "Particular", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})

        # --- SUB-FLUXO PILATES ---
        elif status == "pilates_triagem_modalidade":
            if "Saúde Caixa" in msg_recebida:
                enviar_texto(phone, "Entendido! 🏦 Para o Saúde Caixa, é necessária autorização prévia e pedido médico atualizado.")
                if is_veteran:
                    enviar_texto(phone, "Como já temos seus dados, envie uma FOTO do seu PEDIDO MÉDICO para agilizarmos:")
                    requests.post(WIX_URL, json={"from": phone, "status": "pilates_caixa_pedido"})
                else:
                    enviar_texto(phone, "Para iniciarmos seu cadastro rápido, por favor, digite seu **NOME COMPLETO**:")
                    requests.post(WIX_URL, json={"from": phone, "status": "pilates_caixa_nome"})
            
            elif "Particular" in msg_recebida:
                enviar_texto(phone, "No nosso estúdio você conta com fisioterapeutas especializados e equipamentos de ponta para resultados reais. ✨")
                enviar_texto(phone, "Para podermos passar os detalhes e agendar sua **AULA EXPERIMENTAL**, digite seu **NOME COMPLETO**:")
                requests.post(WIX_URL, json={"from": phone, "status": "pilates_particular_nome"})
            
            elif "Wellhub" in msg_recebida:
                enviar_texto(phone, "Ótimo! ✅ Aceitamos planos **Golden (Wellhub)** e **TP5 (Totalpass)**.")
                enviar_texto(phone, "Para sua autonomia, usamos o **App Next Fit** para agendamentos!\n📱 Android: bit.ly/app-android\n🍎 iPhone: bit.ly/app-iphone")
                enviar_texto(phone, "Para liberarmos seu acesso, digite seu **NOME COMPLETO** e seu **ID do Aplicativo**:")
                requests.post(WIX_URL, json={"from": phone, "status": "pilates_parceria_dados"})

        # --- CADASTRO CAIXA (NOVO) ---
        elif status == "pilates_caixa_nome":
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida, "status": "pilates_caixa_dados_extra"})
            enviar_texto(phone, "Recebido! Agora preciso do seu CPF, Data de Nascimento e E-mail (digite tudo em uma única mensagem):")

        elif status == "pilates_caixa_dados_extra":
            requests.post(WIX_URL, json={"from": phone, "queixa": msg_recebida, "status": "pilates_caixa_carteirinha"})
            enviar_texto(phone, "Obrigado! Agora, envie uma FOTO da sua CARTEIRINHA:")

        elif status == "pilates_caixa_carteirinha":
            requests.post(WIX_URL, json={"from": phone, "status": "pilates_caixa_pedido"})
            enviar_texto(phone, "Quase lá! Agora envie a FOTO do seu PEDIDO MÉDICO:")

        elif status == "pilates_caixa_pedido":
            enviar_texto(phone, "Documentos recebidos! 🎉 Nossa equipe assumirá agora para validar sua autorização e agendar. Aguarde!")
            requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})

        # --- FINALIZAÇÃO PARTICULAR PILATES ---
        elif status == "pilates_particular_nome":
            enviar_texto(phone, f"Prazer, {msg_recebida}! Ficamos muito felizes com seu interesse. O Pilates vai transformar sua postura! 😊")
            enviar_texto(phone, "Nossa equipe assumirá agora para agendar sua **AULA EXPERIMENTAL**. Aguarde um instante!")
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida, "status": "atendimento_humano"})

        # --- PERFORMANCE / RECOVERY ---
        elif status == "performance_nome":
            enviar_texto(phone, "Anotado! Nossa equipe especializada assumirá o atendimento agora para encontrar o melhor horário para você. 👨‍⚕️")
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida, "status": "atendimento_humano"})

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
