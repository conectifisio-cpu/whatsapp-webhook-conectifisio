import os
import json
import requests
import time
import traceback
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
API_KEY = os.environ.get("GEMINI_API_KEY", "") 
WIX_WEBHOOK_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# ==========================================
# FUNÇÕES DE APOIO
# ==========================================

def chamar_gemini(query, system_prompt):
    if not API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": query}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]}
    }
    for delay in [1, 2]:
        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                result = res.json()
                return result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
        except Exception:
            time.sleep(delay)
    return None

def enviar_whatsapp(to, payload_msg):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        **payload_msg
    }
    res = requests.post(url, json=payload, headers=headers, timeout=10)
    print(f"👉 RESPOSTA DA META: {res.status_code} - {res.text}")
    return res

def responder_texto(to, texto):
    return enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

def enviar_botoes(to, texto, botoes):
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {
                "buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"]}} for b in botoes]
            }
        }
    }
    return enviar_whatsapp(to, payload)

def enviar_lista(to, texto, titulo_botao, secoes):
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": texto},
            "action": {
                "button": titulo_botao,
                "sections": secoes
            }
        }
    }
    return enviar_whatsapp(to, payload)

# ==========================================
# WEBHOOK PRINCIPAL (COM RASTREADOR DE ERROS)
# ==========================================

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data:
        return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value:
            return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]
        msg_type = message.get("type")
        
        # 1. LER MENSAGEM DO PACIENTE
        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))

        # 2. SINCRONIZAR COM O WIX CMS
        sync_payload = {"from": phone, "text": msg_recebida}
        try:
            res_wix = requests.post(WIX_WEBHOOK_URL, json=sync_payload, timeout=15)
        except Exception as e:
            responder_texto(phone, f"⚠️ Erro 1: O servidor do Wix não está a responder. Detalhe: {str(e)}")
            return jsonify({"status": "wix_timeout"}), 200
        
        if res_wix.status_code != 200:
            responder_texto(phone, f"⚠️ Erro 2: O Wix devolveu um erro {res_wix.status_code}. Esqueceu-se de Publicar o Wix?")
            return jsonify({"status": "wix_error"}), 200
            
        info = res_wix.json()
        status = info.get("currentStatus", "triagem")
        is_veteran = info.get("isVeteran", False)
        nome_paciente = info.get("patientName", "Paciente")

        # ==========================================
        # 3. LÓGICA DE FLUXO (AS FASES DO BOT)
        # ==========================================

        if status == "atendimento_humano":
            if msg_recebida.lower() in ["reset", "recomeçar", "menu"]:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "triagem"})
                responder_texto(phone, "Entendido! O seu atendimento foi reiniciado. 😊")
            return jsonify({"status": "human_mode"}), 200

        elif status == "triagem" or msg_recebida.lower() in ["oi", "ola", "olá", "bom dia", "boa tarde"]:
            if is_veteran:
                botoes = [
                    {"id": "b1", "title": "🗓️ Reagendar"},
                    {"id": "b2", "title": "🔄 Retomar Tratamento"},
                    {"id": "b3", "title": "➕ Novo Serviço"}
                ]
                responder_texto(phone, f"Olá, {nome_paciente}! ✨ Que bom ter você de volta.")
                enviar_botoes(phone, "Como posso facilitar o seu dia hoje?", botoes)
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                responder_texto(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio.")
                responder_texto(phone, "Para iniciarmos o seu atendimento, como gostaria de ser chamado(a)?")
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "cadastrando_nome"})

        elif status == "cadastrando_nome":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "name": msg_recebida, "status": "cadastrando_queixa"})
            responder_texto(phone, f"Prazer em conhecer, {msg_recebida}! 😊")
            responder_texto(phone, "Me conte brevemente: o que te trouxe à clínica hoje? (Ex: dor nas costas, pós-operatório...)")

        elif status == "cadastrando_queixa":
            prompt = f"Você é fisioterapeuta. Paciente diz: '{msg_recebida}'. Responda em UMA frase curta com extrema empatia."
            acolhimento = chamar_gemini(msg_recebida, prompt) or "Entendi perfeitamente o seu caso. Vamos cuidar disso."
            
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "queixa": msg_recebida, "queixa_ia": acolhimento, "status": "escolha_especialidade"})
            
            secoes = [{
                "title": "Nossos Serviços",
                "rows": [
                    {"id": "s1", "title": "Fisio Ortopédica"},
                    {"id": "s2", "title": "Fisio Neurológica"},
                    {"id": "s3", "title": "Fisio Pélvica"},
                    {"id": "s4", "title": "Acupuntura"},
                    {"id": "s5", "title": "Pilates Studio"},
                    {"id": "s6", "title": "Recovery / Liberação"}
                ]
            }]
            enviar_lista(phone, f"{acolhimento}\n\nPor favor, escolha abaixo a especialidade que você procura hoje:", "Ver Especialidades", secoes)

        elif status == "escolha_especialidade" or status == "menu_veterano":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "servico": msg_recebida, "status": "modalidade"})
            
            if "Pilates" in msg_recebida:
                botoes = [{"id": "p1", "title": "Particular"}, {"id": "p2", "title": "Saúde Caixa"}, {"id": "p3", "title": "Wellhub/Totalpass"}]
                enviar_botoes(phone, "Excelente escolha! 🧘‍♀️ Como pretende realizar as aulas?", botoes)
            elif "Recovery" in msg_recebida:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "modalidade": "Particular", "status": "cpf"})
                responder_texto(phone, "Para este serviço, trabalhamos exclusivamente de forma Particular. ✅\nPor favor, digite o seu CPF (apenas números).")
            else:
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, "Entendido! Deseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)

        elif status == "modalidade":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "modalidade": msg_recebida, "status": "cpf"})
            responder_texto(phone, "Perfeito! Agora, para garantirmos a segurança do seu registro, por favor digite o seu CPF (apenas números).")

        elif status == "cpf":
            cpf_limpo = ''.join(filter(str.isdigit, msg_recebida))
            if len(cpf_limpo) != 11:
                responder_texto(phone, "❌ Este CPF não parece ter 11 números. Por favor, digite novamente (só os números).")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "cpf": cpf_limpo, "status": "data_nascimento"})
                responder_texto(phone, "CPF validado! ✅ Qual a sua data de nascimento? (Ex: 15/05/1980)")

        elif status == "data_nascimento":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "birthDate": msg_recebida, "status": "agendando"})
            botoes = [{"id": "t1", "title": "☀️ Manhã"}, {"id": "t2", "title": "⛅ Tarde"}]
            enviar_botoes(phone, "Quase pronto! Para vermos a disponibilidade, qual o melhor período para você?", botoes)

        elif status == "agendando":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "finalizado"})
            responder_texto(phone, "Tudo pronto! 🎉 Nossa equipe já recebeu seus dados e vai te chamar por aqui em instantes. Até já! 👩‍⚕️")

        return jsonify({"status": "success"}), 200

    except Exception as e:
        # Se o Python crashar, avisa o Dr. Issa diretamente no WhatsApp
        print(f"Erro Crítico: {traceback.format_exc()}")
        responder_texto(phone, f"⚠️ Erro 3 (Cérebro Crashou): {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    hub_mode = request.args.get("hub.mode")
    hub_token = request.args.get("hub.verify_token")
    hub_challenge = request.args.get("hub.challenge")
    if hub_mode == "subscribe" and hub_token == "conectifisio_2024_seguro":
        return hub_challenge, 200
    return "Erro", 403

if __name__ == "__main__":
    app.run(port=5000)
