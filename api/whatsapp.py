import os
import requests
import traceback
import re
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE (Vercel)
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ==========================================
# INICIALIZAÇÃO DO FIREBASE (O Cofre Seguro)
# ==========================================
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
if firebase_creds_json and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_creds_json, strict=False)
        # Desamassa as quebras de linha que a Vercel estraga no JSON
        if 'private_key' in cred_dict:
            cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
            
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        print("Firebase Inicializado com Sucesso!")
    except Exception as e:
        print(f"Erro Crítico ao carregar Firebase: {e}")

db = firestore.client() if firebase_admin._apps else None

# ==========================================
# FUNÇÕES DE MEMÓRIA (FIREBASE)
# ==========================================
def get_paciente(phone):
    """Busca a memória do paciente na base de dados"""
    if not db: return {}
    doc = db.collection("PatientsKanban").document(phone).get()
    return doc.to_dict() if doc.exists else {}

def update_paciente(phone, data):
    """Atualiza a memória do paciente instantaneamente"""
    if not db: return
    data["lastInteraction"] = firestore.SERVER_TIMESTAMP
    db.collection("PatientsKanban").document(phone).set(data, merge=True)

# ==========================================
# FUNÇÕES DE MENSAGERIA E IA
# ==========================================
def chamar_gemini(query, system_prompt):
    """Faz a chamada à Inteligência Artificial do Google"""
    if not API_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"
    payload = {"contents": [{"parts": [{"text": query[:300]}]}], "systemInstruction": {"parts": [{"text": system_prompt}]}}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            return res.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
    except: pass
    return None

def enviar_whatsapp(to, payload_msg):
    """Envia a mensagem genérica para a Meta"""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        return res
    except Exception as e:
        return None

def responder_texto(to, texto):
    return enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

def enviar_botoes(to, texto, botoes):
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]}
        }
    }
    return enviar_whatsapp(to, payload)

def enviar_lista(to, texto, titulo_botao, secoes):
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": texto},
            "action": {"button": titulo_botao[:20], "sections": secoes}
        }
    }
    return enviar_whatsapp(to, payload)

# ==========================================
# WEBHOOK POST (Recebe as mensagens do paciente)
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
        if msg_type == "text": msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type in ["image", "document"]:
            tem_anexo = True
            msg_recebida = "Anexo Recebido"

        # Comando para forçar reinício
        if msg_recebida.lower() in ["recomeçar", "reset", "menu inicial"]:
            update_paciente(phone, {"status": "triagem", "cellphone": phone})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            enviar_botoes(phone, "Atendimento reiniciado. 🔄\n\nEm qual unidade deseja ser atendido?", botoes)
            return jsonify({"status": "reset"}), 200

        # Carrega o contexto do paciente
        info = get_paciente(phone)
        if not info:
            info = {"cellphone": phone, "status": "triagem"}
            update_paciente(phone, info)

        status = info.get("status", "triagem")
        servico = info.get("servico", "")
        cpf_salvo = info.get("cpf", "")
        
        # Identificação de Veterano: se tem CPF válido (11 algarismos)
        is_veteran = True if len(re.sub(r'\D', '', cpf_salvo)) >= 11 else False
        
        # Super Trava: Garante que a modalidade não é perdida
        modalidade = info.get("modalidade", "")
        convenio = info.get("convenio", "")
        if not modalidade and convenio: modalidade = "Convênio"
        elif not modalidade: modalidade = "Particular"

        # Se paciente já finalizou anteriormente e envia nova mensagem
        if status == "finalizado":
            if is_veteran:
                update_paciente(phone, {"status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Reagendar"}, {"id": "v2", "title": "🔄 Retomar"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {info.get('title', 'paciente')}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", botoes)
                return jsonify({"status": "restart_veteran"}), 200
            else:
                status = "triagem"
        
        # -----------------------------------------------------
        # MÁQUINA DE ESTADOS DO CHATBOT
        # -----------------------------------------------------
        if status == "triagem":
            update_paciente(phone, {"status": "escolhendo_unidade"})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            enviar_botoes(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio.\n\nPara iniciarmos, em qual unidade você deseja ser atendido?", botoes)

        elif status == "escolhendo_unidade":
            update_paciente(phone, {"unit": msg_recebida, "status": "cadastrando_nome"})
            responder_texto(phone, f"Unidade {msg_recebida} selecionada! ✅\n\nComo você gostaria de ser chamado(a)?")

        elif status == "cadastrando_nome":
            if is_veteran:
                update_paciente(phone, {"title": msg_recebida, "status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Reagendar"}, {"id": "v2", "title": "🔄 Retomar"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {msg_recebida}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", botoes)
            else:
                update_paciente(phone, {"title": msg_recebida, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"},
                    {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}
                ]}]
                enviar_lista(phone, f"Prazer, {msg_recebida}! 😊\n\nQual serviço você procura hoje?", "Ver Serviços", secoes)

        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                update_paciente(phone, {"status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"},
                    {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}
                ]}]
                enviar_lista(phone, "Perfeito! Qual novo serviço você deseja agendar?", "Ver Serviços", secoes)
            elif "Retomar" in msg_recebida:
                update_paciente(phone, {"status": "modalidade"})
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, "As novas sessões serão pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)
            elif "Reagendar" in msg_recebida:
                update_paciente(phone, {"status": "agendando"})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Certo! Vamos organizar isso. Qual o melhor período para você? ☀️ ⛅", botoes)

        elif status == "escolhendo_especialidade":
            if msg_recebida in ["Recovery", "Liberação Miofascial", "Recovery / Liberação"]:
                update_paciente(phone, {"servico": msg_recebida, "modalidade": "Particular", "status": "cadastrando_queixa"})
                responder_texto(phone, f"Ótima escolha para performance em {msg_recebida}! 🚀\n\nMe conte brevemente: o que te trouxe aqui hoje?")
            elif msg_recebida == "Fisio Neurológica":
                update_paciente(phone, {"servico": msg_recebida, "status": "triagem_neuro"})
                botoes = [{"id": "n1", "title": "🔹 Independente"}, {"id": "n2", "title": "🤝 Semidependente"}, {"id": "n3", "title": "👨‍🦽 Dependente"}]
                enviar_botoes(phone, "Para agendarmos com o especialista ideal, como está a mobilidade do paciente?\n\n🔹 *Independente:* Faz tudo sozinho.\n🤝 *Semidependente:* Precisa de apoio.\n👨‍🦽 *Dependente:* Auxílio constante.", botoes)
            elif msg_recebida == "Pilates Studio":
                update_paciente(phone, {"servico": msg_recebida, "status": "pilates_modalidade"})
                secoes = [{"title": "Modalidade Pilates", "rows": [
                    {"id": "p_part", "title": "💎 Plano Particular"},
                    {"id": "p_caixa", "title": "🏦 Saúde Caixa"},
                    {"id": "p_app", "title": "💪 Wellhub/Totalpass"},
                    {"id": "p_vol", "title": "⬅️ Voltar"}
                ]}]
                enviar_lista(phone, "Excelente escolha! 🧘‍♀️ O Pilates é fundamental para a correção postural e fortalecimento.\n\nPara passarmos as informações corretas de horários e valores, como você pretende realizar as aulas?", "Ver Opções", secoes)
            else:
                update_paciente(phone, {"servico": msg_recebida, "status": "cadastrando_queixa"})
                responder_texto(phone, f"Entendido! {msg_recebida} selecionada.\n\nMe conte brevemente: o que te trouxe à clínica hoje?")

        elif status.startswith("pilates_"):
            if status == "pilates_modalidade":
                if "Voltar" in msg_recebida:
                    update_paciente(phone, {"status": "escolhendo_especialidade"})
                    secoes = [{"title": "Nossos Serviços", "rows": [
                        {"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"},
                        {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"},
                        {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery / Liberação"}
                    ]}]
                    enviar_lista(phone, "Sem problemas! Voltando ao menu de especialidades. Qual serviço você procura hoje?", "Ver Serviços", secoes)
                elif "Wellhub" in msg_recebida or "Totalpass" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Parceria App"})
                    if is_veteran:
                        update_paciente(phone, {"status": "pilates_app"})
                        botoes = [{"id": "w1", "title": "Wellhub"}, {"id": "t1", "title": "Totalpass"}]
                        enviar_botoes(phone, f"Prazer ter você aqui novamente, {info.get('title', 'paciente')}! ✨ E qual desses aplicativos você utiliza para o seu plano?", botoes)
                    else:
                        update_paciente(phone, {"status": "pilates_nome_app"})
                        responder_texto(phone, "Perfeito! ✅ Informamos que para o Pilates aceitamos os planos Golden (Wellhub) e TP5 (Totalpass).\n\nPara iniciarmos o seu atendimento, como gostaria de ser chamado(a)?")
                elif "Saúde Caixa" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Convênio", "convenio": "Saúde Caixa"})
                    if is_veteran:
                        update_paciente(phone, {"status": "pilates_caixa_foto_pedido"})
                        responder_texto(phone, f"Olá, {info.get('title', 'paciente')}! Que bom ver você focado na sua saúde. 🚀 Para seguirmos pelo Saúde Caixa, envie uma FOTO ou PDF do seu PEDIDO MÉDICO atualizado (indicação para Pilates ou Fisioterapia).")
                    else:
                        update_paciente(phone, {"status": "pilates_caixa_nome"})
                        responder_texto(phone, "Entendido! 🏦 Para o plano Saúde Caixa, informamos que é necessária a autorização prévia junto ao plano de saúde. Também é obrigatório apresentar uma solicitação ou pedido médico indicando Pilates ou Fisioterapia.\n\nPara começarmos seu cadastro, digite o seu NOME COMPLETO:")
                elif "Particular" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Particular"})
                    update_paciente(phone, {"status": "pilates_part_exp"})
                    botoes = [{"id": "pe_sim", "title": "Sim, gostaria"}, {"id": "pe_nao", "title": "Não, já quero começar"}]
                    enviar_botoes(phone, "Ótima escolha! No nosso estúdio, você conta com fisioterapeutas altamente especializados e equipamentos de ponta para garantir resultados reais e segurança em cada movimento. ✨\n\nO Pilates vai ajudar a melhorar a sua postura, aliviar dores e fortalecer o corpo todo. Gostaria de agendar uma aula experimental gratuita para conhecer o nosso método e o estúdio?", botoes)

            # -- Fluxo App (Wellhub/Totalpass) --
            elif status == "pilates_nome_app":
                update_paciente(phone, {"title": msg_recebida, "status": "pilates_app"})
                botoes = [{"id": "w1", "title": "Wellhub"}, {"id": "t1", "title": "Totalpass"}]
                enviar_botoes(phone, f"Prazer, {msg_recebida}! E qual desses aplicativos você utiliza para o seu plano?", botoes)

            elif status == "pilates_app":
                update_paciente(phone, {"convenio": msg_recebida})
                if msg_recebida == "Wellhub":
                    update_paciente(phone, {"status": "pilates_wellhub_id"})
                    responder_texto(phone, "Para validarmos o seu acesso, por favor, informe o seu Wellhub ID. Você encontra esse número logo abaixo do seu nome, na secção de perfil do seu aplicativo Wellhub.")
                else:
                    update_paciente(phone, {"status": "pilates_app_pref"})
                    botoes = [{"id": "pa_app", "title": "📱 Usar App"}, {"id": "pa_equipa", "title": "👩‍⚕️ Falar com Equipa"}]
                    enviar_botoes(phone, "Entendido! Para facilitar o seu dia a dia, prefere utilizar o nosso App Exclusivo para gerir os seus horários com total autonomia ou prefere o suporte direto da nossa equipe?", botoes)

            elif status == "pilates_wellhub_id":
                update_paciente(phone, {"numCarteirinha": msg_recebida, "status": "pilates_app_pref"})
                botoes = [{"id": "pa_app", "title": "📱 Usar App"}, {"id": "pa_equipa", "title": "👩‍⚕️ Falar com Equipa"}]
                enviar_botoes(phone, "ID recebido! Para facilitar o seu dia a dia, prefere utilizar o nosso App Exclusivo para gerir os seus horários com total autonomia ou prefere o suporte direto da nossa equipe?", botoes)

            elif status == "pilates_app_pref":
                update_paciente(phone, {"status": "atendimento_humano"})
                if "Usar App" in msg_recebida:
                    responder_texto(phone, "Ótima escolha! Para a sua total comodidade, disponibilizamos um App Exclusivo do Aluno! 📲 Com ele, você ganha autonomia para agendar, cancelar ou remarcar as suas aulas.\n\nBaixe pelos links:\n🍏 iPhone: https://apps.apple.com/app/next-fit/id1451167440\n🤖 Android: https://play.google.com/store/apps/details?id=br.com.nextfit.app\n\nÉ super fácil:\n1️⃣ Abra o app e faça um cadastro rápido\n2️⃣ Selecione a sua cidade\n3️⃣ Busque: Conectifisio - Ictus Fisioterapia SCS\n\nNossa equipe vai assumir o atendimento agora para liberar o seu acesso inicial. Aguarde um instante! 👩‍⚕️")
                else:
                    responder_texto(phone, "Perfeito! Nossa equipe vai assumir o atendimento agora para tirar qualquer dúvida e organizar seus horários. Aguarde um instante! 👩‍⚕️")

            # -- Fluxo Saúde Caixa --
            elif status == "pilates_caixa_nome":
                update_paciente(phone, {"title": msg_recebida, "status": "pilates_caixa_cpf"})
                responder_texto(phone, "Nome registrado! ✅ Agora, digite seu CPF (apenas os 11 números):")
                
            elif status == "pilates_caixa_cpf":
                cpf_limpo = re.sub(r'\D', '', msg_recebida)
                if len(cpf_limpo) != 11:
                    responder_texto(phone, "❌ CPF inválido. Digite apenas os 11 números.")
                else:
                    update_paciente(phone, {"cpf": cpf_limpo, "status": "pilates_caixa_nasc"})
                    responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")

            elif status == "pilates_caixa_nasc":
                update_paciente(phone, {"birthDate": msg_recebida, "status": "pilates_caixa_email"})
                responder_texto(phone, "Ótimo! Qual seu melhor E-MAIL?")
                
            elif status == "pilates_caixa_email":
                update_paciente(phone, {"email": msg_recebida, "status": "pilates_caixa_foto_cart"})
                responder_texto(phone, "Anotado! ✅ Agora a parte documental:\n\nEnvie uma FOTO NÍTIDA da sua carteirinha Saúde Caixa (use o ícone de clipe ou câmera).")

            elif status == "pilates_caixa_foto_cart":
                if not tem_anexo:
                    responder_texto(phone, "❌ Não recebi a imagem. Por favor, envie a foto da sua carteirinha.")
                else:
                    update_paciente(phone, {"status": "pilates_caixa_foto_pedido", "tem_foto_carteirinha": True})
                    responder_texto(phone, "Foto recebida! ✅\n\nAgora, envie a FOTO ou PDF DO SEU PEDIDO MÉDICO.")

            elif status == "pilates_caixa_foto_pedido":
                if not tem_anexo:
                    responder_texto(phone, "❌ Por favor, envie a foto ou PDF do seu Pedido Médico.")
                else:
                    update_paciente(phone, {"status": "atendimento_humano", "tem_foto_pedido": True})
                    responder_texto(phone, "Dados e documentos recebidos com sucesso! Nossa equipe vai assumir o atendimento agora para dar andamento ao seu processo. Aguarde um instante! 👩‍⚕️")

            # -- Fluxo Particular --
            elif status == "pilates_part_exp":
                update_paciente(phone, {"interesse_experimental": msg_recebida, "status": "pilates_part_periodo"})
                botoes = [{"id": "pe_m", "title": "☀️ Manhã"}, {"id": "pe_t", "title": "⛅ Tarde"}, {"id": "pe_n", "title": "🌙 Noite"}]
                if "Sim" in msg_recebida:
                    enviar_botoes(phone, "Agradecemos muito pela sua escolha! Ficamos muito felizes em ter você conosco.\n\nPara agilizarmos o agendamento da sua aula experimental, qual o melhor período para você?", botoes)
                else:
                    enviar_botoes(phone, "Excelente escolha! Vamos direto para a agenda.\n\nPara agilizarmos, qual o melhor período para você?", botoes)

            elif status == "pilates_part_periodo":
                update_paciente(phone, {"periodo": msg_recebida})
                if is_veteran:
                    update_paciente(phone, {"status": "atendimento_humano"})
                    responder_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para encontrar o melhor horário. Aguarde um instante! 👩‍⚕️")
                else:
                    update_paciente(phone, {"status": "pilates_part_nome"})
                    responder_texto(phone, "Para finalizarmos seu cadastro e liberarmos a agenda, por favor, digite seu NOME COMPLETO:")
                    
            elif status == "pilates_part_nome":
                update_paciente(phone, {"title": msg_recebida, "status": "pilates_part_cpf"})
                responder_texto(phone, "Nome registrado! ✅ Agora, digite seu CPF (apenas os 11 números):")
                
            elif status == "pilates_part_cpf":
                cpf_limpo = re.sub(r'\D', '', msg_recebida)
                if len(cpf_limpo) != 11:
                    responder_texto(phone, "❌ CPF inválido. Digite apenas os 11 números.")
                else:
                    update_paciente(phone, {"cpf": cpf_limpo, "status": "pilates_part_nasc"})
                    responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")

            elif status == "pilates_part_nasc":
                update_paciente(phone, {"birthDate": msg_recebida, "status": "pilates_part_email"})
                responder_texto(phone, "Para completarmos, qual seu melhor E-MAIL?")

            elif status == "pilates_part_email":
                update_paciente(phone, {"email": msg_recebida, "status": "atendimento_humano"})
                responder_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para confirmar o seu horário e início. Aguarde um instante! 👩‍⚕️")

        elif status == "triagem_neuro":
            if "Dependente" in msg_recebida and "Semi" not in msg_recebida:
                update_paciente(phone, {"mobilidade": msg_recebida, "status": "atendimento_humano"})
                responder_texto(phone, "Devido à complexidade do caso, nosso fisioterapeuta responsável entrará em contato agora para te dar atenção total. Aguarde um instante! 👨‍⚕️")
            else:
                update_paciente(phone, {"mobilidade": msg_recebida, "status": "cadastrando_queixa"})
                responder_texto(phone, "Anotado! ✅\n\nMe conte brevemente: o que te trouxe à clínica hoje?")

        elif status == "cadastrando_queixa":
            prompt = "Você é fisioterapeuta no Brasil. Paciente relatou dor. Responda com UMA frase curta e empática."
            acolhimento = chamar_gemini(msg_recebida, prompt) or "Sinto muito por isso, vamos cuidar de você."
            
            if servico in ["Recovery", "Liberação Miofascial"]:
                if is_veteran:
                    update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "agendando"})
                    botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                    enviar_botoes(phone, f"{acolhimento}\n\nComo você já é nosso paciente, pulei o cadastro! Vamos direto para a agenda. Qual o melhor período para você? ☀️ ⛅", botoes)
                else:
                    update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "cadastrando_nome_completo"})
                    responder_texto(phone, f"{acolhimento}\n\nPara iniciarmos seu cadastro, por favor digite seu NOME COMPLETO (conforme documento):")
            else:
                update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "modalidade"})
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, f"{acolhimento}\n\nDeseja atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)

        elif status == "modalidade":
            if "Convênio" in msg_recebida:
                update_paciente(phone, {"modalidade": "Convênio", "status": "nome_convenio"})
                secoes = [{"title": "Convênios Aceitos", "rows": [
                    {"id": "c1", "title": "Saúde Petrobras"}, {"id": "c2", "title": "Mediservice"},
                    {"id": "c3", "title": "Cassi"}, {"id": "c4", "title": "Geap Saúde"},
                    {"id": "c5", "title": "Amil"}, {"id": "c6", "title": "Bradesco Saúde"},
                    {"id": "c7", "title": "Bradesco Operadora"}, {"id": "c8", "title": "Porto Seguro"},
                    {"id": "c9", "title": "Prevent Senior"}, {"id": "c10", "title": "Saúde Caixa"}
                ]}]
                enviar_lista(phone, "Selecione o seu plano de saúde para validarmos a cobertura:", "Ver Convênios", secoes)
            else:
                if is_veteran:
                    update_paciente(phone, {"modalidade": "Particular", "status": "agendando"})
                    botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                    enviar_botoes(phone, "Perfeito! Como você já é nosso paciente, vamos direto para a agenda. Qual o melhor período para você? ☀️ ⛅", botoes)
                else:
                    update_paciente(phone, {"modalidade": "Particular", "status": "cadastrando_nome_completo"})
                    responder_texto(phone, "Perfeito! Para seu cadastro particular, digite seu NOME COMPLETO (conforme documento):")

        elif status == "nome_convenio":
            if is_veteran:
                update_paciente(phone, {"convenio": msg_recebida, "status": "foto_carteirinha"})
                responder_texto(phone, f"Anotado: {msg_recebida}! ✅\n\nComo você já é nosso paciente, pulei o preenchimento de CPF e E-mail! Mas como é um novo serviço, por favor, envie uma FOTO NÍTIDA da sua carteirinha do plano.")
            else:
                update_paciente(phone, {"convenio": msg_recebida, "status": "cadastrando_nome_completo"})
                responder_texto(phone, f"Anotado: {msg_recebida}! ✅\n\nAgora, digite seu NOME COMPLETO (conforme documento):")

        elif status == "cadastrando_nome_completo":
            update_paciente(phone, {"title": msg_recebida, "status": "cpf"})
            responder_texto(phone, "Nome registrado! ✅ Agora, digite seu CPF (apenas os 11 números):")

        elif status == "cpf":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) != 11:
                responder_texto(phone, "❌ CPF inválido. Digite apenas os 11 números, sem pontos ou traços.")
            else:
                update_paciente(phone, {"cpf": cpf_limpo, "status": "data_nascimento"})
                responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")

        elif status == "data_nascimento":
            update_paciente(phone, {"birthDate": msg_recebida, "status": "coletando_email"})
            responder_texto(phone, "Ótimo! Para finalizar seu cadastro, qual seu melhor E-MAIL?")

        elif status == "coletando_email":
            if modalidade == "Particular":
                update_paciente(phone, {"email": msg_recebida, "status": "agendando"})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Cadastro concluído! 🎉\n\nQual o melhor período para verificarmos a agenda particular?", botoes)
            else:
                update_paciente(phone, {"email": msg_recebida, "status": "num_carteirinha"})
                responder_texto(phone, "Certo! E qual o NÚMERO DA CARTEIRINHA do seu plano? (apenas números)")

        elif status == "num_carteirinha":
            num_limpo = re.sub(r'\D', '', msg_recebida)
            update_paciente(phone, {"numCarteirinha": num_limpo, "status": "foto_carteirinha"})
            responder_texto(phone, "Anotado! ✅ Agora a parte documental:\n\nEnvie uma FOTO NÍTIDA da sua carteirinha (use o ícone de clipe ou câmera do WhatsApp).")

        elif status == "foto_carteirinha":
            if not tem_anexo: 
                responder_texto(phone, "❌ Não recebi a imagem. Por favor, envie a foto da sua carteirinha.")
            else:
                update_paciente(phone, {"status": "foto_pedido_medico", "tem_foto_carteirinha": True})
                responder_texto(phone, "Foto recebida! ✅\n\nAgora, envie a FOTO DO SEU PEDIDO MÉDICO.")

        elif status == "foto_pedido_medico":
            if not tem_anexo: 
                responder_texto(phone, "❌ Por favor, envie a foto do seu Pedido Médico.")
            else:
                update_paciente(phone, {"status": "agendando", "tem_foto_pedido": True})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Documentação completa! 🎉\n\nQual o melhor período para verificarmos a sua vaga?", botoes)

        elif status == "agendando":
            if msg_recebida in ["Manhã", "Tarde"]:
                update_paciente(phone, {"periodo": msg_recebida, "status": "finalizado"})
                responder_texto(phone, f"Horário de {msg_recebida} pré-agendado com sucesso! ✅ Nossa recepção vai finalizar a autorização e confirmar tudo em instantes.")
            else:
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Por favor, utilize os botões abaixo para escolher o período: ☀️ ⛅", botoes)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"Erro Crítico POST: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 200

# ==========================================
# WEBHOOK GET (Trata verificação Meta e Pedido do Dashboard)
# ==========================================
@app.route("/api/whatsapp", methods=["GET"])
def verify_or_data():
    # 1. Porta de Verificação do WhatsApp (Meta)
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
        
    # 2. A "Porta Secreta" para o Dashboard ler o Firebase
    if request.args.get("action") == "get_patients":
        try:
            if not db: return jsonify({"items": []}), 200
            docs = db.collection("PatientsKanban").stream()
            patients = []
            for doc in docs:
                data = doc.to_dict()
                data["id"] = doc.id
                # Converte a data do Firebase para string
                if "lastInteraction" in data and data["lastInteraction"]:
                    try:
                        data["lastInteraction"] = data["lastInteraction"].isoformat()
                    except:
                        data["lastInteraction"] = str(data["lastInteraction"])
                patients.append(data)
            return jsonify({"items": patients}), 200
        except Exception as e:
            print(f"Erro ao buscar pacientes: {str(e)}")
            return jsonify({"error": str(e), "items": []}), 500
            
    return "Acesso Negado ou Rota Incorreta", 403

if __name__ == "__main__":
    app.run(port=5000)
