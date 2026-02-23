import os
import requests
import traceback
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES (Lê da Vercel)
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
WIX_WEBHOOK_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# ==========================================
# FUNÇÕES DE APOIO E MENSAGERIA
# ==========================================
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

# ==========================================
# WEBHOOK PRINCIPAL (O CÉREBRO)
# ==========================================
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

        # -----------------------------------------------------
        # 1. COMANDOS DE EMERGÊNCIA (FUNCIONAM SEMPRE)
        # -----------------------------------------------------
        if msg_recebida.lower() in ["reset", "recomeçar", "recomecar", "menu inicial"]:
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "triagem"})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            responder_texto(phone, "Entendido! O seu atendimento foi reiniciado do zero. 🔄\n\nOlá! ✨ Seja muito bem-vindo à Conectifisio.")
            enviar_botoes(phone, "Para iniciarmos, em qual unidade deseja ser atendido(a)?", botoes)
            return jsonify({"status": "reset"}), 200

        if msg_recebida == "Sim, continuar":
            responder_texto(phone, "Ótimo! Por favor, responda à etapa anterior ou escolha uma opção para darmos sequência.")
            return jsonify({"status": "resume"}), 200

        # ESCUDO ANTI-SAUDAÇÃO LIXO
        if msg_recebida.lower() in ["oi", "ola", "olá", "bom dia", "boa tarde", "boa noite", "tudo bem"]:
            sync_payload = {"from": phone}
            try:
                res_wix = requests.post(WIX_WEBHOOK_URL, json=sync_payload, timeout=10)
                status = res_wix.json().get("currentStatus", "triagem")
                if status not in ["triagem", "finalizado", "atendimento_humano"]:
                    botoes = [{"id": "r1", "title": "Sim, continuar"}, {"id": "r2", "title": "Recomeçar"}]
                    enviar_botoes(phone, "Olá! ✨ Notei que estávamos no meio do seu atendimento. Podemos continuar de onde paramos ou prefere recomeçar?", botoes)
                    return jsonify({"status": "paused"}), 200
            except: pass

        # -----------------------------------------------------
        # 2. SINCRONIZAÇÃO COM O WIX (O GUARDIÃO DE ESTADO)
        # -----------------------------------------------------
        sync_payload = {"from": phone, "text": msg_recebida}
        try:
            res_wix = requests.post(WIX_WEBHOOK_URL, json=sync_payload, timeout=15)
        except Exception as e:
            responder_texto(phone, "⚠️ Erro de conexão com a clínica. Aguarde um instante e tente enviar 'Oi' novamente.")
            return jsonify({"status": "wix_timeout"}), 200
            
        info = res_wix.json()
        status = info.get("currentStatus", "triagem")
        is_veteran = info.get("isVeteran", False)
        nome_paciente = info.get("patientName", "Paciente")

        if status == "atendimento_humano":
            return jsonify({"status": "human_mode"}), 200

        # -----------------------------------------------------
        # 3. O MAPA MESTRE DE FLUXO
        # -----------------------------------------------------

        # FASE 1: UNIDADE
        if status == "triagem":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "escolhendo_unidade"})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            responder_texto(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio.")
            enviar_botoes(phone, "Para iniciarmos, em qual unidade você deseja ser atendido(a)?", botoes)

        # FASE 2: NOME
        elif status == "escolhendo_unidade":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "unit": msg_recebida, "status": "cadastrando_nome"})
            responder_texto(phone, "Ótima escolha! Para continuarmos, como você gostaria de ser chamado(a)?")

        # FASE 3: BIFURCAÇÃO (NOVO VS VETERANO)
        elif status == "cadastrando_nome":
            # Salva o nome e determina o próximo passo
            if is_veteran:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "name": msg_recebida, "status": "menu_veterano"})
                botoes = [
                    {"id": "v1", "title": "🗓️ Reagendar"}, 
                    {"id": "v2", "title": "🔄 Retomar Tratamento"}, 
                    {"id": "v3", "title": "➕ Novo Serviço"}
                ]
                enviar_botoes(phone, f"Olá, {msg_recebida}! ✨ Que bom ter você de volta conosco. Como podemos facilitar o seu dia hoje?", botoes)
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "name": msg_recebida, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"},
                    {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery / Liberação"}
                ]}]
                responder_texto(phone, f"Prazer em conhecer você, {msg_recebida}! 😊")
                enviar_lista(phone, "Por favor, escolha abaixo a especialidade que você procura hoje:", "Ver Especialidades", secoes)

        # -----------------------------------
        # ROTA EXCLUSIVA VETERANO
        # -----------------------------------
        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"},
                    {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery / Liberação"}
                ]}]
                enviar_lista(phone, "Perfeito! Qual o novo serviço que você deseja agendar?", "Ver Especialidades", secoes)
            
            elif "Retomar" in msg_recebida or "Continuar" in msg_recebida:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "veterano_modalidade"})
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, "Ótimo que vai dar continuidade! 🚀\nAs novas sessões serão pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)
                
            elif "Reagendar" in msg_recebida:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "buscando_vagas"})
                botoes = [{"id": "t1", "title": "☀️ Manhã"}, {"id": "t2", "title": "⛅ Tarde"}, {"id": "t3", "title": "🌙 Noite"}]
                enviar_botoes(phone, "Certo! Vamos organizar isso. Qual o melhor período para você?", botoes)
            
            else:
                # Fallback se digitar algo errado
                botoes = [
                    {"id": "v1", "title": "🗓️ Reagendar"}, 
                    {"id": "v2", "title": "🔄 Retomar Tratamento"}, 
                    {"id": "v3", "title": "➕ Novo Serviço"}
                ]
                enviar_botoes(phone, "Por favor, escolha uma das opções abaixo para eu poder te ajudar:", botoes)

        elif status == "veterano_modalidade":
            # Salva Modalidade e vai direto para agendamento! (Pula Queixa, Pula CPF, Pula Nascimento)
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "modalidade": msg_recebida, "status": "buscando_vagas"})
            botoes = [{"id": "t1", "title": "☀️ Manhã"}, {"id": "t2", "title": "⛅ Tarde"}, {"id": "t3", "title": "🌙 Noite"}]
            enviar_botoes(phone, "Perfeito! E qual o melhor período da sua preferência para as próximas sessões?", botoes)

        # -----------------------------------
        # ROTA DE NOVOS PACIENTES
        # -----------------------------------
        elif status == "escolhendo_especialidade":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "servico": msg_recebida, "status": "cadastrando_queixa"})
            responder_texto(phone, "Entendido! Me conte brevemente: o que te trouxe à clínica hoje? (Ex: dor na lombar, pós-operatório...)")

        elif status == "cadastrando_queixa":
            # IA de Acolhimento
            prompt = f"Você é fisioterapeuta no Brasil. O paciente relatou: '{msg_recebida}'. Responda com UMA frase curta e muito empática, sem dar diagnósticos."
            acolhimento = chamar_gemini(msg_recebida, prompt) or "Sinto muito por isso, vamos cuidar de você."
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "queixa": msg_recebida, "queixa_ia": acolhimento, "status": "modalidade"})
            
            botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
            enviar_botoes(phone, f"{acolhimento}\n\nDeseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)

        elif status == "modalidade":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "modalidade": msg_recebida, "status": "cpf"})
            responder_texto(phone, "Perfeito! Para garantirmos a segurança do seu cadastro, por favor, digite o seu CPF (apenas números).")

        elif status == "cpf":
            # Limpeza e Validação Estrita do CPF
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) != 11:
                responder_texto(phone, "❌ O CPF informado parece incorreto. Por favor, digite os 11 números sem pontos ou traços.")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "cpf": cpf_limpo, "status": "data_nascimento"})
                responder_texto(phone, "CPF validado! ✅ Qual a sua data de nascimento? (Ex: 15/05/1980)")

        elif status == "data_nascimento":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "birthDate": msg_recebida, "status": "buscando_vagas"})
            botoes = [{"id": "t1", "title": "☀️ Manhã"}, {"id": "t2", "title": "⛅ Tarde"}, {"id": "t3", "title": "🌙 Noite"}]
            enviar_botoes(phone, "Quase lá! Para verificarmos a disponibilidade na nossa agenda, qual o melhor período para você?", botoes)

        # -----------------------------------
        # FECHAMENTO COMUM (Novo e Veterano)
        # -----------------------------------
        elif status == "buscando_vagas":
            # Posta para o Wix com a ação "get_slots" para bater na API do Feegow
            res = requests.post(WIX_WEBHOOK_URL, json={"from": phone, "periodo": msg_recebida, "action": "get_slots", "status": "oferecendo_horarios"})
            dados = res.json()
            slots = dados.get("slots", [])
            
            if slots and len(slots) >= 2:
                botoes = [{"id": f"h_{s['id']}", "title": s['time']} for s in slots[:2]]
                botoes.append({"id": "h_outros", "title": "Outros Horários"})
                enviar_botoes(phone, f"Encontrei estas vagas para o período da {msg_recebida}. Alguma fica boa para você?", botoes)
            else:
                # Fallback Silencioso se não achar vagas na API
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "agendando"})
                responder_texto(phone, "Tudo pronto! 🎉 A nossa equipe recebeu os seus dados e vai verificar a agenda com cuidado. Entramos em contato por aqui em instantes para confirmar o horário exato. Até já! 👩‍⚕️")

        elif status == "oferecendo_horarios":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "agendando"})
            if "Outros" in msg_recebida:
                responder_texto(phone, "Entendido! A nossa equipe assumirá o atendimento para procurar um horário perfeito para si. Aguarde um instante! 👩‍⚕️")
            else:
                responder_texto(phone, f"Horário das {msg_recebida} pré-agendado com sucesso! ✅ A nossa recepção vai finalizar a autorização e confirmar tudo em instantes.")

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
