import os
import requests
import traceback
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES (Vercel)
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
# URL direta para evitar erros de configuração na Vercel
WIX_WEBHOOK_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# ==========================================
# FUNÇÕES DE APOIO (MENSAGERIA E IA)
# ==========================================
def chamar_gemini(query, system_prompt):
    """Chama a IA para gerar empatia na queixa do paciente"""
    if not API_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": query[:300]}]}], 
        "systemInstruction": {"parts": [{"text": system_prompt}]}
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            return res.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
    except: pass
    return None

def enviar_whatsapp(to, payload_msg):
    """Envia a requisição para a API da Meta"""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    return requests.post(url, json=payload, headers=headers, timeout=10)

def responder_texto(to, texto):
    return enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

def enviar_botoes(to, texto, botoes):
    """Envia até 3 botões interativos"""
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
    """Envia menu de lista (até 10 opções)"""
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
        tem_anexo = False
        if msg_type == "text": 
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type in ["image", "document"]:
            tem_anexo = True
            msg_recebida = "Anexo Recebido"

        # -----------------------------------------------------
        # 1. COMANDOS GLOBAIS (RESET)
        # -----------------------------------------------------
        if msg_recebida.lower() in ["recomeçar", "reset", "menu", "menu inicial"]:
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "triagem"})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            enviar_botoes(phone, "Entendido! Vamos recomeçar seu atendimento. 🔄\n\nOlá! ✨ Seja muito bem-vindo à Conectifisio. Em qual unidade você deseja ser atendido?", botoes)
            return jsonify({"status": "reset"}), 200

        # -----------------------------------------------------
        # 2. CONSULTA ESTADO NO WIX (QUEM É ESTE PACIENTE?)
        # -----------------------------------------------------
        res_wix = requests.post(WIX_WEBHOOK_URL, json={"from": phone}, timeout=10)
        info = res_wix.json()
        status = info.get("currentStatus", "triagem")
        is_veteran = info.get("isVeteran", False)
        modalidade = info.get("modalidade", "Particular")

        # -----------------------------------------------------
        # 3. ROTEAMENTO DO FLUXO (BÍBLIA v85.0)
        # -----------------------------------------------------
        
        # FASE 1: UNIDADE
        if status == "triagem":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "escolhendo_unidade"})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            enviar_botoes(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio. Para iniciarmos, em qual unidade você deseja ser atendido?", botoes)

        # FASE 2: NOME (APÓS UNIDADE)
        elif status == "escolhendo_unidade":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "unit": msg_recebida, "status": "cadastrando_nome"})
            responder_texto(phone, f"Ótima escolha! Unidade {msg_recebida} selecionada. Para continuarmos, como você gostaria de ser chamado(a)?")

        # FASE 3: BIFURCAÇÃO (VETERANO VS NOVO)
        elif status == "cadastrando_nome":
            if is_veteran:
                # Se for veterano (CPF/Celular já existe no Wix), abre o menu de autoatendimento
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "name": msg_recebida, "status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Reagendar"}, {"id": "v2", "title": "🔄 Retomar Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {msg_recebida}! ✨ Que bom ter você de volta conosco. Como podemos facilitar o seu dia hoje?", botoes)
            else:
                # Se for novo, abre o menu de especialidades unificado
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "name": msg_recebida, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"}, 
                    {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"}, 
                    {"id": "e4", "title": "Acupuntura"},
                    {"id": "e5", "title": "Pilates Studio"}, 
                    {"id": "e6", "title": "Recovery / Liberação Miofascial"}
                ]}]
                enviar_lista(phone, f"Prazer em conhecer você, {msg_recebida}! 😊\n\nQual especialidade você procura hoje?", "Ver Especialidades", secoes)

        # ROTA VETERANO
        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"},
                    {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery / Liberação Miofascial"}
                ]}]
                enviar_lista(phone, "Perfeito! Qual novo serviço você deseja agendar?", "Ver Especialidades", secoes)
            elif "Retomar" in msg_recebida or "Continuar" in msg_recebida:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "modalidade"})
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, "Ótimo que vai dar continuidade! 🚀\nAs novas sessões serão pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)
            elif "Reagendar" in msg_recebida:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "buscando_vagas"})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Certo! Vamos organizar isso. Qual o melhor período para você? ☀️ ⛅", botoes)

        # ROTA NOVO PACIENTE (QUEIXA E IA)
        elif status == "escolhendo_especialidade":
            # Atalho para Recovery/Liberação (Sempre Particular)
            if "Recovery" in msg_recebida:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "servico": "Recovery / Liberação Miofascial", "modalidade": "Particular", "status": "cadastrando_queixa"})
                responder_texto(phone, "Excelente escolha para sua performance! 🚀 Me conte brevemente: o que te trouxe aqui hoje?")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "servico": msg_recebida, "status": "cadastrando_queixa"})
                responder_texto(phone, f"Entendido! {msg_recebida} selecionada. Me conte brevemente: o que te trouxe à clínica hoje? (Ex: dor nas costas, pós-operatório...)")

        elif status == "cadastrando_queixa":
            # Inteligência de Acolhimento
            prompt = "Você é fisioterapeuta na Conectifisio Brasil. Responda com UMA frase curta de acolhimento empático para a dor relatada."
            acolhimento = chamar_gemini(msg_recebida, prompt) or "Sinto muito por isso, vamos cuidar de você."
            
            if info.get("servico") == "Recovery / Liberação Miofascial":
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "queixa": msg_recebida, "queixa_ia": acolhimento, "status": "cpf"})
                responder_texto(phone, f"{acolhimento}\n\nPara iniciarmos seu cadastro particular, digite o seu CPF (apenas números).")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "queixa": msg_recebida, "queixa_ia": acolhimento, "status": "modalidade"})
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, f"{acolhimento}\n\nDeseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)

        # FASE 4: BUROCRACIA BIFURCADA
        elif status == "modalidade":
            if "Convênio" in msg_recebida:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "modalidade": "Convênio", "status": "nome_convenio"})
                responder_texto(phone, "Entendido! Qual é o nome do seu plano de saúde? (Ex: Amil, Bradesco, SulAmérica...)")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "modalidade": "Particular", "status": "cpf"})
                responder_texto(phone, "Perfeito! Para iniciarmos seu cadastro particular, digite o seu CPF (apenas números).")

        elif status == "nome_convenio":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "convenio": msg_recebida, "status": "cpf"})
            responder_texto(phone, "Anotado! Agora, digite o seu CPF (apenas números).")

        elif status == "cpf":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) != 11:
                responder_texto(phone, "❌ O CPF deve ter 11 números. Digite novamente apenas os números.")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "cpf": cpf_limpo, "status": "data_nascimento"})
                responder_texto(phone, "CPF validado! ✅ Qual a sua data de nascimento? (Ex: 15/05/1980)")

        elif status == "data_nascimento":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "birthDate": msg_recebida, "status": "coletando_email"})
            responder_texto(phone, "Perfeito! Digite o seu melhor E-mail (usamos para o envio das notas e lembretes).")

        elif status == "coletando_email":
            if modalidade == "Particular":
                # Particular termina aqui e vai para agenda
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "email": msg_recebida, "status": "buscando_vagas"})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Cadastro concluído! 🎉 Qual o melhor período para verificarmos a disponibilidade na agenda? ☀️ ⛅", botoes)
            else:
                # Convênio pede o número da carteirinha
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "email": msg_recebida, "status": "num_carteirinha"})
                responder_texto(phone, "Certo! E qual é o NÚMERO DA SUA CARTEIRINHA do convênio? (apenas números)")

        elif status == "num_carteirinha":
            num_limpo = re.sub(r'\D', '', msg_recebida)
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "numCarteirinha": num_limpo, "status": "foto_carteirinha"})
            responder_texto(phone, "Ótimo! Agora a parte documental:\n\nPor favor, envie uma FOTO NÍTIDA DA SUA CARTEIRINHA do plano.")

        elif status == "foto_carteirinha":
            if not tem_anexo: responder_texto(phone, "❌ Não recebi a imagem. Clique no clipe 📎 ou câmera 📷 e envie a foto da sua carteirinha.")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "foto_pedido_medico"})
                responder_texto(phone, "Foto recebida! ✅ Agora envie a FOTO DO SEU PEDIDO MÉDICO.")

        elif status == "foto_pedido_medico":
            if not tem_anexo: responder_texto(phone, "❌ Por favor, envie a foto do seu Pedido Médico.")
            else:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "buscando_vagas"})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Documentação completa! 🎉 Para verificarmos a disponibilidade na nossa agenda, qual o melhor período para você? ☀️ ⛅", botoes)

        # FASE 5: CONSULTA AGENDA REAL (FEEGOW VIA WIX)
        elif status == "buscando_vagas":
            # Período limpo sem emojis para o Wix bater na API do Feegow
            periodo = msg_recebida.replace("☀️", "").replace("⛅", "").strip()
            res = requests.post(WIX_WEBHOOK_URL, json={"from": phone, "periodo": periodo, "action": "get_slots", "status": "oferecendo_horarios"})
            slots = res.json().get("slots", [])
            
            if slots and len(slots) >= 2:
                # Regra das 2 opções
                botoes = [{"id": f"h_{s['id']}", "title": s['time']} for s in slots[:2]]
                botoes.append({"id": "h_outros", "title": "Outros Horários"})
                enviar_botoes(phone, f"Encontrei estas vagas reais no Feegow para o período da {periodo}. Alguma fica boa para você?", botoes)
            else:
                # Fallback de segurança se a agenda falhar
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "agendando"})
                responder_texto(phone, "Tudo pronto! 🎉 Nossa equipe recebeu seus dados e sua documentação. Vamos confirmar o horário exato no Feegow e te chamamos em instantes. Até já! 👩‍⚕️")

        elif status == "oferecendo_horarios":
            requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "agendando"})
            responder_texto(phone, f"Horário de {msg_recebida} pré-agendado com sucesso! ✅ Nossa recepção vai finalizar a autorização e confirmar tudo em instantes.")

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"Erro Crítico: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 200

# VERIFICAÇÃO DO WEBHOOK (META)
@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403

if __name__ == "__main__":
    app.run(port=5000)
