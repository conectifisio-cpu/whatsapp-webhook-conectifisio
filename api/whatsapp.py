import os
import json
import requests
import time
import traceback
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# CONFIGURAÇÕES DA VERCEL
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
WIX_WEBHOOK_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# FUNÇÕES DO WHATSAPP
def chamar_gemini(query, system_prompt):
    if not API_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"
    payload = {"contents": [{"parts": [{"text": query}]}], "systemInstruction": {"parts": [{"text": system_prompt}]}}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            return res.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
    except: pass
    return None

def enviar_whatsapp(to, payload_msg):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    return requests.post(url, json=payload, headers=headers, timeout=10)

def responder_texto(to, texto):
    return enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

def enviar_botoes(to, texto, botoes):
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"]}} for b in botoes]}
        }
    }
    return enviar_whatsapp(to, payload)

def enviar_lista(to, texto, titulo_botao, secoes):
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": texto},
            "action": {"button": titulo_botao, "sections": secoes}
        }
    }
    return enviar_whatsapp(to, payload)

# WEBHOOK PRINCIPAL DO FLUXO
@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]
        msg_type = message.get("type")
        
        msg_recebida = ""
        if msg_type == "text": msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))

        # ESCUDO DE SAUDAÇÃO LIXO
        if msg_recebida.lower() in ["oi", "ola", "olá", "bom dia", "boa tarde", "tudo bem"]:
            # Só ignora se for no meio de um cadastro para não quebrar o funil
            sync_payload = {"from": phone} # Apenas consulta
            res_wix = requests.post(WIX_WEBHOOK_URL, json=sync_payload, timeout=10)
            status = res_wix.json().get("currentStatus", "triagem")
            if status not in ["triagem", "finalizado", "atendimento_humano"]:
                botoes = [{"id": "r1", "title": "Sim, continuar"}, {"id": "r2", "title": "Recomeçar"}]
                enviar_botoes(phone, "Olá! ✨ Notei que estávamos no meio do seu atendimento. Podemos continuar de onde paramos?", botoes)
                return jsonify({"status": "paused"}), 200

        # SINCRONIZA COM WIX
        sync_payload = {"from": phone, "text": msg_recebida}
        try:
            res_wix = requests.post(WIX_WEBHOOK_URL, json=sync_payload, timeout=15)
        except:
            responder_texto(phone, "⚠️ Erro de conexão. Aguarde um instante e tente novamente.")
            return jsonify({"status": "wix_timeout"}), 200
            
        info = res_wix.json()
        status = info.get("currentStatus", "triagem")
        is_veteran = info.get("isVeteran", False)
        nome_paciente = info.get("patientName", "Paciente")

        # -----------------------------------------------------
        # O MAPA MESTRE (FLUXO)
        # -----------------------------------------------------

        if status == "atendimento_humano":
            if msg_recebida.lower() in ["reset", "recomeçar", "menu"]:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "triagem"})
                responder_texto(phone, "Entendido! O seu atendimento foi reiniciado. 😊")
            return jsonify({"status": "human_mode"}), 200

        # FASE 1: UNIDADE
        elif status == "triagem":
            if msg_recebida.lower() == "recomeçar": msg_recebida = ""
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "escolhendo_unidade"})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            responder_texto(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio.")
            enviar_botoes(phone, "Para iniciarmos o seu atendimento, em qual unidade você deseja ser atendido?", botoes)

        # FASE 2: NOME E VETERANO
        elif status == "escolhendo_unidade":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "unit": msg_recebida, "status": "cadastrando_nome"})
            responder_texto(phone, "Ótima escolha! Para continuarmos, como você gostaria de ser chamado(a)?")

        elif status == "cadastrando_nome":
            if msg_recebida == "Sim, continuar": msg_recebida = nome_paciente # Bypass de saudação
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "name": msg_recebida, "status": "escolhendo_especialidade"})
            
            if is_veteran:
                botoes = [{"id": "v1", "title": "🗓️ Reagendar"}, {"id": "v2", "title": "🔄 Retomar Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {msg_recebida}! ✨ Que bom ter você de volta conosco. Como podemos facilitar o seu dia hoje?", botoes)
            else:
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"},
                    {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery / Liberação"}
                ]}]
                responder_texto(phone, f"Prazer em conhecer você, {msg_recebida}! 😊")
                enviar_lista(phone, "Por favor, escolha abaixo a especialidade que você procura hoje:", "Ver Especialidades", secoes)

        # FASE 3: QUEIXA E IA
        elif status == "escolhendo_especialidade" or (status == "escolhendo_especialidade" and is_veteran):
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "servico": msg_recebida, "status": "cadastrando_queixa"})
            responder_texto(phone, "Entendido! Me conte brevemente: o que te trouxe à clínica hoje? (Ex: dor na lombar, pós-operatório...)")

        # FASE 4: MODALIDADE
        elif status == "cadastrando_queixa":
            # IA de Acolhimento
            prompt = f"Você é fisioterapeuta no Brasil. Paciente relatou: '{msg_recebida}'. Responda com UMA frase curta e muito empática."
            acolhimento = chamar_gemini(msg_recebida, prompt) or "Sinto muito por isso, vamos cuidar de você."
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "queixa": msg_recebida, "queixa_ia": acolhimento, "status": "modalidade"})
            
            botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
            enviar_botoes(phone, f"{acolhimento}\n\nDeseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)

        # FASE 5: CPF (Escada de Dados)
        elif status == "modalidade":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "modalidade": msg_recebida, "status": "cpf"})
            responder_texto(phone, "Perfeito! Para garantirmos a segurança do seu cadastro, por favor, digite o seu CPF (apenas números).")

        # FASE 6: DATA DE NASCIMENTO
        elif status == "cpf":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) != 11:
                responder_texto(phone, "❌ O CPF deve ter 11 números. Por favor, digite novamente sem pontos ou traços.")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "cpf": cpf_limpo, "status": "data_nascimento"})
                responder_texto(phone, "CPF validado! ✅ Qual a sua data de nascimento? (Ex: 15/05/1980)")

        # FASE 7: PERÍODO
        elif status == "data_nascimento":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "birthDate": msg_recebida, "status": "buscando_vagas"})
            botoes = [{"id": "t1", "title": "☀️ Manhã"}, {"id": "t2", "title": "⛅ Tarde"}, {"id": "t3", "title": "🌙 Noite"}]
            enviar_botoes(phone, "Quase lá! Para verificarmos a disponibilidade na nossa agenda, qual o melhor período para você?", botoes)

        # FASE 8: INTEGRAÇÃO FEEGOW (A REGRA DOS 2 HORÁRIOS)
        elif status == "buscando_vagas":
            # Salva o período e pede os slots para o Wix
            res = requests.post(WIX_WEBHOOK_URL, json={"from": phone, "periodo": msg_recebida, "action": "get_slots", "status": "oferecendo_horarios"})
            dados = res.json()
            slots = dados.get("slots", [])
            
            if slots and len(slots) >= 2:
                # O Feegow retornou as vagas! Mostra 2 botões.
                botoes = [{"id": f"h_{s['id']}", "title": s['time']} for s in slots[:2]]
                botoes.append({"id": "h_outros", "title": "Outros Horários"})
                enviar_botoes(phone, f"Encontrei estas vagas para o período da {msg_recebida}. Alguma fica boa para você?", botoes)
            else:
                # Fallback Silencioso: Se a agenda Feegow falhar, não trava o bot!
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "agendando"})
                responder_texto(phone, "Tudo pronto! 🎉 Nossa equipe recebeu seus dados e vai verificar a agenda com cuidado. Te chamamos por aqui em instantes para confirmar o horário exato. Até já! 👩‍⚕️")

        # FASE 9: CONFIRMAÇÃO DO HORÁRIO
        elif status == "oferecendo_horarios":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "agendando"})
            if "Outros" in msg_recebida:
                responder_texto(phone, "Entendido! Nossa equipe assumirá o atendimento para buscar um horário perfeito para você. Aguarde um instante! 👩‍⚕️")
            else:
                responder_texto(phone, f"Horário de {msg_recebida} pré-agendado com sucesso! ✅ Nossa recepção vai finalizar a autorização e confirmar tudo em instantes.")

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"Erro Crítico: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403

if __name__ == "__main__":
    app.run(port=5000)
