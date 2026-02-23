import os
import requests
import traceback
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
WIX_WEBHOOK_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

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
        tem_anexo = False
        if msg_type == "text": 
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type in ["image", "document"]:
            tem_anexo = True
            msg_recebida = "Anexo Recebido"

        # 1. BOTÃO DE EMERGÊNCIA
        if msg_type == "text" and msg_recebida.lower() in ["reset", "recomeçar", "recomecar", "menu inicial"]:
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "triagem"})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            responder_texto(phone, "Entendido! O seu atendimento foi reiniciado do zero. 🔄\n\nOlá! ✨ Seja muito bem-vindo à Conectifisio.")
            enviar_botoes(phone, "Para iniciarmos, em qual unidade deseja ser atendido(a)?", botoes)
            return jsonify({"status": "reset"}), 200

        if msg_recebida == "Sim, continuar":
            responder_texto(phone, "Ótimo! Por favor, responda à pergunta anterior ou escolha uma opção.")
            return jsonify({"status": "resume"}), 200

        # ESCUDO ANTI-LIXO
        if msg_type == "text" and msg_recebida.lower() in ["oi", "ola", "olá", "bom dia", "boa tarde", "boa noite", "tudo bem"]:
            sync_payload = {"from": phone}
            try:
                res_wix = requests.post(WIX_WEBHOOK_URL, json=sync_payload, timeout=10)
                status = res_wix.json().get("currentStatus", "triagem")
                if status not in ["triagem", "finalizado", "atendimento_humano"]:
                    botoes = [{"id": "r1", "title": "Sim, continuar"}, {"id": "r2", "title": "Recomeçar"}]
                    enviar_botoes(phone, "Olá! ✨ Notei que estávamos no meio do seu cadastro. Podemos continuar de onde paramos ou prefere recomeçar?", botoes)
                    return jsonify({"status": "paused"}), 200
            except: pass

        # 2. SINCRONIZAÇÃO COM O WIX
        sync_payload = {"from": phone, "text": msg_recebida, "tem_anexo": tem_anexo}
        try:
            res_wix = requests.post(WIX_WEBHOOK_URL, json=sync_payload, timeout=15)
        except Exception as e:
            responder_texto(phone, "⚠️ Erro de conexão com a clínica. Aguarde um instante e envie 'Oi' novamente.")
            return jsonify({"status": "wix_timeout"}), 200
            
        info = res_wix.json()
        status = info.get("currentStatus", "triagem")
        is_veteran = info.get("isVeteran", False)
        # Traz a modalidade do banco para saber qual rota seguir depois
        modalidade = info.get("modalidade", "Convênio")

        if status == "atendimento_humano":
            return jsonify({"status": "human_mode"}), 200

        # -----------------------------------------------------
        # O MAPA DE FLUXO (NOVOS E VETERANOS)
        # -----------------------------------------------------
        
        if status == "triagem":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "escolhendo_unidade"})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            responder_texto(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio.")
            enviar_botoes(phone, "Para iniciarmos, em qual unidade você deseja ser atendido(a)?", botoes)

        elif status == "escolhendo_unidade":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "unit": msg_recebida, "status": "cadastrando_nome"})
            responder_texto(phone, "Ótima escolha! Para continuarmos, como você gostaria de ser chamado(a)?")

        elif status == "cadastrando_nome":
            if is_veteran:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "name": msg_recebida, "status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Reagendar"}, {"id": "v2", "title": "🔄 Retomar Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}]
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

        # ROTA VETERANO (Pula burocracia)
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
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                enviar_botoes(phone, "Certo! Vamos organizar isso. Qual o melhor período para você? ☀️ ⛅ 🌙", botoes)
            
            else:
                botoes = [{"id": "v1", "title": "🗓️ Reagendar"}, {"id": "v2", "title": "🔄 Retomar Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, "Por favor, escolha uma das opções abaixo para eu poder te ajudar:", botoes)

        elif status == "veterano_modalidade":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "modalidade": msg_recebida, "status": "buscando_vagas"})
            botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
            enviar_botoes(phone, "Perfeito! E qual o melhor período da sua preferência para as próximas sessões? ☀️ ⛅ 🌙", botoes)

        # ROTA NOVOS PACIENTES (BIFURCAÇÃO DA BUROCRACIA)
        elif status == "escolhendo_especialidade":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "servico": msg_recebida, "status": "cadastrando_queixa"})
            responder_texto(phone, "Entendido! Me conte brevemente: o que te trouxe à clínica hoje? (Ex: dor na lombar, pós-operatório, prevenção...)")

        elif status == "cadastrando_queixa":
            prompt = f"Você é fisioterapeuta no Brasil. O paciente relatou: '{msg_recebida}'. Responda com UMA frase curta e muito empática, sem dar diagnósticos."
            acolhimento = chamar_gemini(msg_recebida, prompt) or "Puxa, sinto muito por isso. Vamos cuidar de você."
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "queixa": msg_recebida, "queixa_ia": acolhimento, "status": "modalidade"})
            
            botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
            enviar_botoes(phone, f"{acolhimento}\n\nDeseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)

        # -----------------------------------------------------
        # A BIFURCAÇÃO (PARTICULAR vs CONVÊNIO)
        # -----------------------------------------------------
        elif status == "modalidade":
            if "Convênio" in msg_recebida:
                # Se for Convênio, pede o nome do plano de saúde
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "modalidade": "Convênio", "status": "nome_convenio"})
                responder_texto(phone, "Entendido! Para que o seu convênio libere o tratamento, precisamos de alguns dados.\n\nQual é o nome do seu plano de saúde? (Ex: Amil, Bradesco...)")
            else:
                # Se for Particular, PULA o nome do convênio e vai direto pro CPF
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "modalidade": "Particular", "status": "cpf"})
                responder_texto(phone, "Ótimo! Para iniciarmos o seu cadastro particular, por favor, digite o seu CPF (apenas números).")

        # ROTA DO CONVÊNIO APENAS
        elif status == "nome_convenio":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "convenio": msg_recebida, "status": "cpf"})
            responder_texto(phone, "Anotado! Agora, digite o seu CPF (apenas números).")

        # AMBOS (Convênio e Particular)
        elif status == "cpf":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) != 11:
                responder_texto(phone, "❌ O CPF informado parece incorreto. Por favor, digite os 11 números sem pontos ou traços.")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "cpf": cpf_limpo, "status": "data_nascimento"})
                responder_texto(phone, "CPF validado! ✅ Qual a sua data de nascimento? (Ex: 15/05/1980)")

        # AMBOS (Convênio e Particular)
        elif status == "data_nascimento":
            # Agora ambos precisam informar o E-mail!
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "birthDate": msg_recebida, "status": "coletando_email"})
            responder_texto(phone, "Perfeito! Por favor, digite o seu melhor E-mail (usamos para o envio das notas e recibos).")

        # -----------------------------------------------------
        # A SEGUNDA BIFURCAÇÃO (E-MAIL FINALIZA O PARTICULAR)
        # -----------------------------------------------------
        elif status == "coletando_email":
            if modalidade == "Particular":
                # O Particular termina aqui e vai agendar!
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "email": msg_recebida, "status": "buscando_vagas"})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                enviar_botoes(phone, "Cadastro concluído! 🎉 Para verificarmos a disponibilidade na nossa agenda particular, qual o melhor período para você? ☀️ ⛅ 🌙", botoes)
            else:
                # O Convênio continua para enviar profissão e fotos!
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "email": msg_recebida, "status": "coletando_profissao"})
                responder_texto(phone, "Certo! E qual é a sua profissão atual? (Essa informação é exigida pela ANS na ficha clínica).")

        # ROTA EXCLUSIVA CONVÊNIO (Fotos)
        elif status == "coletando_profissao":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "profissao": msg_recebida, "status": "foto_carteirinha"})
            responder_texto(phone, "Ótimo! Agora a parte mais importante:\n\nPara liberarmos a autorização no seu convênio, preciso que você envie uma *FOTO NÍTIDA DA SUA CARTEIRINHA* do plano de saúde.\n\n📷 (Pode tirar a foto e enviar aqui agora mesmo).")

        elif status == "foto_carteirinha":
            if not tem_anexo:
                responder_texto(phone, "❌ Não recebi a imagem. Por favor, clique no botão de Anexo 📎 ou Câmera 📷 no seu WhatsApp e envie a foto da sua carteirinha do convênio.")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "foto_pedido_medico"})
                responder_texto(phone, "Foto da carteirinha recebida! ✅\n\nPor último, preciso que você envie a *FOTO DO PEDIDO MÉDICO* (Ele precisa ter sido emitido há no máximo 60 dias).\n\n📷 Aguardo o envio para procurarmos os horários na agenda!")

        elif status == "foto_pedido_medico":
            if not tem_anexo:
                responder_texto(phone, "❌ Não recebi a imagem. Por favor, envie a foto ou PDF do seu Pedido Médico.")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "buscando_vagas"})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                enviar_botoes(phone, "Documentação completa! 🎉 Para verificarmos a disponibilidade na nossa agenda, qual o melhor período para você? ☀️ ⛅ 🌙", botoes)

        # -----------------------------------------------------
        # FECHAMENTO (AGENDA FEEGOW PARA AMBOS)
        # -----------------------------------------------------
        elif status == "buscando_vagas":
            res = requests.post(WIX_WEBHOOK_URL, json={"from": phone, "periodo": msg_recebida, "action": "get_slots", "status": "oferecendo_horarios"})
            dados = res.json()
            slots = dados.get("slots", [])
            
            if slots and len(slots) >= 2:
                botoes = [{"id": f"h_{s['id']}", "title": s['time']} for s in slots[:2]]
                botoes.append({"id": "h_outros", "title": "Outros Horários"})
                enviar_botoes(phone, f"Encontrei estas vagas para o período da {msg_recebida}. Alguma fica boa para você?", botoes)
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "agendando"})
                responder_texto(phone, "Tudo pronto! Nossa equipe vai gerar o agendamento no sistema. A recepção vai te chamar por aqui em instantes para confirmar o horário exato. Até já! 👩‍⚕️")

        elif status == "oferecendo_horarios":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "agendando"})
            if "Outros" in msg_recebida:
                responder_texto(phone, "Entendido! A nossa equipe assumirá o atendimento para buscar um horário perfeito para você. Aguarde um instante! 👩‍⚕️")
            else:
                responder_texto(phone, f"Horário das {msg_recebida} pré-agendado com sucesso! ✅ A recepção vai finalizar o processo e confirmar tudo em instantes.")

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
