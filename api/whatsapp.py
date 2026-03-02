import os
import json
import traceback
import re
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore
import requests

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")

# ==========================================
# INICIALIZAÇÃO FIREBASE 
# ==========================================
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")

if firebase_creds_json and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_creds_json, strict=False)
        if 'private_key' in cred_dict:
            cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        print(f"❌ ERRO FIREBASE INIT: {e}")

db = firestore.client() if firebase_admin._apps else None

def update_paciente(phone, data):
    if not db: return "Erro: Banco offline"
    try:
        data["lastInteraction"] = firestore.SERVER_TIMESTAMP
        db.collection("PatientsKanban").document(phone).set(data, merge=True)
        return "OK"
    except Exception as e:
        return str(e)

# ==========================================
# MENSAGERIA INTERATIVA (BOTÕES E LISTAS)
# ==========================================
def simular_digitacao():
    time.sleep(1.0)

def enviar_whatsapp(to, payload):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    try: requests.post(url, json={"messaging_product": "whatsapp", "to": to, **payload}, headers=headers, timeout=10)
    except: pass

def enviar_botoes(to, texto, botoes):
    simular_digitacao()
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": [{"type": "reply", "reply": {"id": f"b_{i}", "title": b[:20]}} for i, b in enumerate(botoes)]}
        }
    }
    enviar_whatsapp(to, payload)

def enviar_lista(to, texto, titulo_botao, secoes):
    simular_digitacao()
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
    enviar_whatsapp(to, payload)

def enviar_texto(to, texto):
    simular_digitacao()
    enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

# ==========================================
# LISTAS OFICIAIS (MENUS SUSPENSOS)
# ==========================================
def get_secoes_especialidades():
    return [
        {
            "title": "Tratamento Clínico",
            "rows": [
                {"id": "s1", "title": "Fisio Ortopédica"},
                {"id": "s2", "title": "Fisio Neurológica"},
                {"id": "s3", "title": "Fisio Pélvica"},
                {"id": "s4", "title": "Acupuntura"}
            ]
        },
        {
            "title": "Bem-Estar e Estúdio",
            "rows": [
                {"id": "s5", "title": "Pilates Studio"},
                {"id": "s6", "title": "Recovery"},
                {"id": "s7", "title": "Liberação Miofascial"}
            ]
        }
    ]

def get_secoes_veterano():
    return [
        {
            "title": "Opções de Atendimento",
            "rows": [
                {"id": "v1", "title": "🗓️ Reagendar Sessão"},
                {"id": "v2", "title": "🔄 Nova Guia"},
                {"id": "v3", "title": "➕ Novo Serviço"},
                {"id": "v4", "title": "📁 Outras Solicitações"}
            ]
        }
    ]

# ==========================================
# CÉREBRO MASTER (A MÁQUINA DE ESTADOS)
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
        msg_type = message.get("type", "text")
        
        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            inter = message.get("interactive", {})
            if inter.get("type") == "button_reply":
                msg_recebida = inter.get("button_reply", {}).get("title", "")
            elif inter.get("type") == "list_reply":
                msg_recebida = inter.get("list_reply", {}).get("title", "")
                
        msg_lower = msg_recebida.lower().strip()

        # 🛡️ BARREIRA DE ÁUDIO E LIGAÇÕES
        if msg_type == "audio":
            enviar_texto(phone, "Ainda não consigo ouvir áudios por aqui 🎧. Para que eu possa te ajudar agora, por favor use os botões ou digite sua resposta em texto.")
            return jsonify({"status": "ok"}), 200
        if msg_type in ["system", "unsupported", "unknown"]:
            return jsonify({"status": "ok"}), 200

        # IDENTIFICAÇÃO AUTOMÁTICA DE UNIDADE
        display_phone = value.get("metadata", {}).get("display_phone_number", "")
        unidade_auto = "Ipiranga" if "23629360" in str(display_phone) else "SCS"

        # LER MEMÓRIA DO FIREBASE
        info = {}
        if db:
            doc_ref = db.collection("PatientsKanban").document(phone)
            doc = doc_ref.get()
            info = doc.to_dict() if doc.exists else {}

        status = info.get("status", "inicio")
        is_veterano = len(re.sub(r'\D', '', info.get("cpf", ""))) >= 11

        # BOTÃO DE PÂNICO / RESET
        if msg_lower in ["reset", "recomeçar", "menu inicial"]:
            if db: doc_ref.delete()
            enviar_botoes(phone, f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unidade_auto}. Esta é a unidade que deseja atendimento ou prefere trocar?", ["✅ Continuar aqui", "📍 Trocar Unidade"])
            update_paciente(phone, {"status": "confirmar_unidade", "unit": unidade_auto, "cellphone": phone})
            return jsonify({"status": "ok"}), 200

        # ESCUDO ANTI-LIXO (Se mandar um "Oi" a meio do processo)
        if status not in ["inicio", "confirmar_unidade", "aguardando_nome", "menu_veterano", "finalizado", "atendimento_humano"]:
            if msg_lower in ["oi", "olá", "ola", "bom dia", "boa tarde", "tudo bem"]:
                enviar_botoes(phone, "Notei que estávamos no meio do seu atendimento. Podemos continuar de onde paramos?", ["✅ Sim, continuar", "🔄 Recomeçar"])
                return jsonify({"status": "ok"}), 200
            if msg_lower == "✅ sim, continuar":
                enviar_texto(phone, "Perfeito! Por favor, me dê a sua última resposta para avançarmos. 😊")
                return jsonify({"status": "ok"}), 200
            elif msg_lower == "🔄 recomeçar":
                if db: doc_ref.delete()
                enviar_botoes(phone, f"Vamos recomeçar! Você deseja atendimento na unidade {unidade_auto} ou prefere trocar?", ["✅ Continuar aqui", "📍 Trocar Unidade"])
                update_paciente(phone, {"status": "confirmar_unidade", "unit": unidade_auto})
                return jsonify({"status": "ok"}), 200

        # ========================================================
        # 🚪 FASE 1: A PORTA DE ENTRADA
        # ========================================================
        if status == "inicio":
            enviar_botoes(phone, f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unidade_auto}. Esta é a unidade que deseja atendimento ou prefere trocar?", ["✅ Continuar aqui", "📍 Trocar Unidade"])
            update_paciente(phone, {"status": "confirmar_unidade", "unit": unidade_auto, "cellphone": phone})
            return jsonify({"status": "ok"}), 200

        elif status == "confirmar_unidade":
            if "trocar" in msg_lower:
                nova_unidade = "Ipiranga" if info.get("unit") == "SCS" else "SCS"
                enviar_texto(phone, f"Sem problemas! Transferimos para a unidade {nova_unidade}. ✅\n\nPara começarmos o seu atendimento e garantirmos o seu cadastro, por favor, digite o seu **NOME COMPLETO**:")
                update_paciente(phone, {"status": "aguardando_nome", "unit": nova_unidade})
            elif "continuar" in msg_lower or "aqui" in msg_lower or "sim" in msg_lower:
                enviar_texto(phone, "Perfeito! ✅ Para começarmos o seu atendimento e garantirmos o seu cadastro, por favor, digite o seu **NOME COMPLETO**:")
                update_paciente(phone, {"status": "aguardando_nome"})
            else:
                enviar_botoes(phone, "Por favor, utilize os botões abaixo para confirmar a unidade:", ["✅ Continuar aqui", "📍 Trocar Unidade"])
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_nome":
            nome_completo = msg_recebida.title()
            primeiro_nome = nome_completo.split()[0]
            update_paciente(phone, {"title": nome_completo})
            
            if is_veterano:
                enviar_lista(phone, f"Olá, {primeiro_nome}! ✨ Que bom ter você de volta na Conectifisio. Em que posso te ajudar hoje?", "Abrir Menu", get_secoes_veterano())
                update_paciente(phone, {"status": "menu_veterano"})
            else:
                enviar_lista(phone, f"Prazer em conhecer, {primeiro_nome}! 😊 Para direcionarmos o seu atendimento, qual serviço você procura hoje?", "Ver Serviços", get_secoes_especialidades())
                update_paciente(phone, {"status": "aguardando_servico"})
            return jsonify({"status": "ok"}), 200

        # ========================================================
        # 🔄 FASE 2: O FLUXO DO VETERANO
        # ========================================================
        elif status == "menu_veterano":
            if "reagendar" in msg_lower:
                enviar_botoes(phone, "Entendido! Vamos organizar isso. Qual o melhor período para você?", ["☀️ Manhã", "⛅ Tarde"])
                update_paciente(phone, {"status": "aguardando_data_remarcacao"})
            elif "nova guia" in msg_lower or "continuar" in msg_lower:
                enviar_botoes(phone, "Excelente! As novas sessões serão pelo seu CONVÊNIO ou PARTICULAR?", ["💳 Convênio", "💎 Particular"])
                update_paciente(phone, {"status": "veterano_modalidade"})
            elif "novo" in msg_lower:
                enviar_lista(phone, "Perfeito! Qual novo serviço você deseja conhecer?", "Ver Serviços", get_secoes_especialidades())
                update_paciente(phone, {"status": "aguardando_servico"})
            elif "outras" in msg_lower:
                enviar_lista(phone, "Certo! Qual solicitação administrativa você precisa?", "Opções", [{"title": "Solicitações", "rows": [{"id": "o1", "title": "📄 Atestado"}, {"id": "o2", "title": "📝 Relatório"}, {"id": "o3", "title": "👤 Recepção"}]}])
                update_paciente(phone, {"status": "outras_solicitacoes"})
            else:
                enviar_lista(phone, "Por favor, selecione uma opção no menu:", "Abrir Menu", get_secoes_veterano())
            return jsonify({"status": "ok"}), 200

        elif status == "outras_solicitacoes":
            enviar_texto(phone, "Solicitação registrada! ✅ Nossa equipe de recepção vai providenciar isso para você e te chamará em instantes. 👩‍⚕️")
            update_paciente(phone, {"status": "atendimento_humano", "queixa": f"[SOLICITAÇÃO]: {msg_recebida}"})
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_data_remarcacao":
            enviar_texto(phone, "Recebido com sucesso! ✅ Nossa equipe vai confirmar a agenda e te envia a confirmação final por aqui. Até já! 👩‍⚕️")
            update_paciente(phone, {"status": "atendimento_humano", "queixa": f"[REMARCAÇÃO PEDIDA]: {msg_recebida}"})
            return jsonify({"status": "ok"}), 200

        elif status == "veterano_modalidade":
            if "particular" in msg_lower:
                enviar_botoes(phone, "Ótimo! Vamos direto para a agenda. Qual período você prefere para as sessões?", ["☀️ Manhã", "⛅ Tarde", "🌙 Noite"])
                update_paciente(phone, {"status": "aguardando_periodo", "modalidade": "Particular"})
            elif "conv" in msg_lower:
                enviar_botoes(phone, "Você continua utilizando o MESMO CONVÊNIO ou houve alguma mudança no seu plano?", ["✅ Mesmo Convênio", "🔄 Troquei de Plano"])
                update_paciente(phone, {"status": "veterano_check_plano", "modalidade": "Convênio"})
            else:
                enviar_botoes(phone, "Por favor, escolha uma das opções abaixo:", ["💳 Convênio", "💎 Particular"])
            return jsonify({"status": "ok"}), 200

        elif status == "veterano_check_plano":
            if "troquei" in msg_lower:
                enviar_texto(phone, "Entendido! Vamos atualizar o sistema. Qual o NOME do seu NOVO convênio?")
                update_paciente(phone, {"status": "aguardando_nome_plano"})
            elif "mesmo" in msg_lower:
                enviar_texto(phone, "Perfeito! Você já está com o NOVO PEDIDO MÉDICO em mãos? Se sim, por favor envie uma FOTO ou PDF dele agora. 📸")
                update_paciente(phone, {"status": "aguardando_foto_pedido"})
            else:
                enviar_botoes(phone, "Por favor, utilize os botões:", ["✅ Mesmo Convênio", "🔄 Troquei de Plano"])
            return jsonify({"status": "ok"}), 200

        # ========================================================
        # 📋 FASE 3: MENU DE ESPECIALIDADES E TRAVAS
        # ========================================================
        elif status == "aguardando_servico":
            servico = ""
            if "ortop" in msg_lower: servico = "Fisio Ortopédica"
            elif "neuro" in msg_lower: servico = "Fisio Neurológica"
            elif "pélvic" in msg_lower or "pelvic" in msg_lower: servico = "Fisio Pélvica"
            elif "acupuntura" in msg_lower: servico = "Acupuntura"
            elif "pilates" in msg_lower: servico = "Pilates Studio"
            elif "recovery" in msg_lower: servico = "Recovery"
            elif "libera" in msg_lower: servico = "Liberação Miofascial"
            
            if not servico:
                enviar_lista(phone, "Por favor, utilize o botão 'Ver Serviços' para escolher a especialidade. 😊", "Ver Serviços", get_secoes_especialidades())
                return jsonify({"status": "ok"}), 200

            update_paciente(phone, {"servico": servico})

            # ATALHO PREMIUM (Recovery / Liberação) - Pula Convênio
            if servico in ["Recovery", "Liberação Miofascial"]:
                update_paciente(phone, {"modalidade": "Particular"})
                enviar_texto(phone, f"Ótima escolha! O serviço de {servico} é focado em alta performance e realizado de forma particular. 💎")
                if is_veterano:
                    enviar_botoes(phone, "Como você já é nosso paciente, vamos direto para a agenda! Qual o melhor período?", ["☀️ Manhã", "⛅ Tarde", "🌙 Noite"])
                    update_paciente(phone, {"status": "aguardando_periodo_particular_antes"})
                else:
                    enviar_texto(phone, "Para iniciarmos o seu cadastro de forma ágil, digite seu CPF (apenas os 11 números, sem pontos ou traços):")
                    update_paciente(phone, {"status": "aguardando_cpf"})
                return jsonify({"status": "ok"}), 200

            # TRAVA DE UNIDADE PILATES
            if servico == "Pilates Studio":
                if info.get("unit") == "Ipiranga":
                    enviar_botoes(phone, "O Pilates Studio é uma modalidade exclusiva da unidade SCS. 🧘‍♀️ Deseja transferir o seu atendimento para lá?", ["✅ Mudar p/ SCS", "❌ Escolher outro"])
                    update_paciente(phone, {"status": "pilates_transferencia"})
                else:
                    enviar_botoes(phone, "Excelente escolha! 🧘‍♀️ O Pilates é fundamental. Como você pretende realizar as aulas?", ["💎 Particular", "🏦 Saúde Caixa", "💪 Wellhub/Totalpass"])
                    update_paciente(phone, {"status": "pilates_modalidade"})
                return jsonify({"status": "ok"}), 200
                
            # TRIAGEM NEURO (Didática)
            if servico == "Fisio Neurológica":
                msg_neuro = (
                    "Para garantirmos o especialista ideal e direcionarmos você para a agenda correta, precisamos entender a mobilidade do paciente:\n\n"
                    "🔹 *Independente:* Realiza as atividades de forma autônoma e segura.\n"
                    "🤝 *Semidependente:* Precisa de ajuda parcial ou uso de apoio (bengala/andador).\n"
                    "👨‍🦽 *Dependente:* Precisa de auxílio integral para se locomover."
                )
                enviar_botoes(phone, msg_neuro, ["🔹 Independente", "🤝 Semidependente", "👨‍🦽 Dependente"])
                update_paciente(phone, {"status": "triagem_neuro"})
                return jsonify({"status": "ok"}), 200
            
            # FLUXO CLÍNICO NORMAL
            enviar_texto(phone, "Entendido! Me conte brevemente: o que te trouxe à clínica hoje? (Ex: dor lombar, pós-cirúrgico...)")
            update_paciente(phone, {"status": "aguardando_queixa"})
            return jsonify({"status": "ok"}), 200

        # ========================================================
        # 🧘‍♀️ FASE 4: FLUXOS ISOLADOS (Pilates)
        # ========================================================
        elif status == "pilates_transferencia":
            if "mudar" in msg_lower or "sim" in msg_lower:
                enviar_botoes(phone, "Unidade alterada para SCS! ✅ Como você pretende realizar as aulas de Pilates?", ["💎 Particular", "🏦 Saúde Caixa", "💪 Wellhub/Totalpass"])
                update_paciente(phone, {"status": "pilates_modalidade", "unit": "SCS"})
            else:
                enviar_lista(phone, "Sem problemas! Qual outro serviço você procura?", "Ver Serviços", get_secoes_especialidades())
                update_paciente(phone, {"status": "aguardando_servico"})
            return jsonify({"status": "ok"}), 200

        elif status == "pilates_modalidade":
            if "particular" in msg_lower:
                enviar_botoes(phone, "No nosso estúdio, você conta com especialistas de ponta! ✨ Gostaria de agendar uma AULA EXPERIMENTAL GRATUITA?", ["Sim, gostaria", "Não, quero começar"])
                update_paciente(phone, {"status": "pilates_experimental", "modalidade": "Particular"})
            elif "caixa" in msg_lower:
                enviar_texto(phone, "Entendido! 🏦 Para Saúde Caixa, é obrigatório pedido médico (Pilates ou Fisioterapia). Para iniciarmos o seu cadastro de liberação, digite seu CPF (apenas 11 números):")
                update_paciente(phone, {"status": "aguardando_cpf", "modalidade": "Convênio", "convenio": "Saúde Caixa"})
            elif "wellhub" in msg_lower or "totalpass" in msg_lower or "app" in msg_lower:
                update_paciente(phone, {"modalidade": "Parceria App"})
                if is_veterano:
                    enviar_texto(phone, f"Ótimo ter você com a gente novamente! ✨\n\nPara validarmos o seu acesso, por favor, informe o seu **Wellhub ID** ou **Token do Totalpass** (você encontra essa numeração no seu perfil do aplicativo):")
                    update_paciente(phone, {"status": "aguardando_id_parceiro"})
                else:
                    enviar_texto(phone, "Perfeito! Aceitamos Wellhub e Totalpass. Para o seu cadastro inicial obrigatório, por favor, digite seu CPF (apenas 11 números):")
                    update_paciente(phone, {"status": "aguardando_cpf"})
            else:
                enviar_botoes(phone, "Por favor, escolha uma das modalidades:", ["💎 Particular", "🏦 Saúde Caixa", "💪 Wellhub/Totalpass"])
            return jsonify({"status": "ok"}), 200

        elif status == "pilates_experimental":
            enviar_botoes(phone, "Excelente! Para agilizarmos a sua agenda, qual o melhor período para você?", ["☀️ Manhã", "⛅ Tarde", "🌙 Noite"])
            update_paciente(phone, {"status": "aguardando_periodo_particular_antes"})
            return jsonify({"status": "ok"}), 200
            
        elif status == "aguardando_periodo_particular_antes":
            update_paciente(phone, {"periodo": msg_recebida})
            if is_veterano:
                enviar_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para te confirmar o horário exato. Aguarde um instante! 👩‍⚕️")
                update_paciente(phone, {"status": "finalizado"})
            else:
                enviar_texto(phone, f"Período registrado! ✅ Agora, para finalizarmos seu cadastro, digite seu CPF (apenas 11 números):")
                update_paciente(phone, {"status": "aguardando_cpf"})
            return jsonify({"status": "ok"}), 200

        elif status == "triagem_neuro":
            if "dependente" in msg_lower and "semi" not in msg_lower:
                enviar_texto(phone, "Entendido. Devido à complexidade e para garantirmos o melhor cuidado, nosso fisioterapeuta assumirá o seu atendimento agora. Aguarde um instante! 👨‍⚕️")
                update_paciente(phone, {"status": "atendimento_humano", "queixa_ia": "[ALERTA: PACIENTE DEPENDENTE NEURO]"})
                return jsonify({"status": "ok"}), 200
            elif "independente" in msg_lower or "semi" in msg_lower:
                enviar_texto(phone, "Perfeito! Me conte brevemente: qual a principal queixa ou diagnóstico do paciente?")
                update_paciente(phone, {"status": "aguardando_queixa"})
                return jsonify({"status": "ok"}), 200
            else:
                msg_neuro_erro = "Por favor, utilize os botões para indicar a mobilidade:\n🔹 Independente\n🤝 Semidependente\n👨‍🦽 Dependente"
                enviar_botoes(phone, msg_neuro_erro, ["🔹 Independente", "🤝 Semidependente", "👨‍🦽 Dependente"])
                return jsonify({"status": "ok"}), 200

        # ========================================================
        # 🪜 FASE 5: A ESCADA DE DADOS E BUROCRACIA
        # ========================================================
        elif status == "aguardando_queixa":
            update_paciente(phone, {"queixa": msg_recebida})
            enviar_texto(phone, "Compreendo perfeitamente a sua situação. Fique tranquilo que vamos avaliar a melhor forma de cuidar de você para aliviar essa dor! 💙")
            simular_digitacao()
            enviar_botoes(phone, "Para seguirmos com o agendamento, deseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", ["💳 Convênio", "💎 Particular"])
            update_paciente(phone, {"status": "escolha_modalidade"})
            return jsonify({"status": "ok"}), 200

        elif status == "escolha_modalidade":
            if "particular" in msg_lower:
                enviar_botoes(phone, "Ótima escolha! Para agilizarmos, qual o melhor período para você?", ["☀️ Manhã", "⛅ Tarde", "🌙 Noite"])
                update_paciente(phone, {"status": "aguardando_periodo_particular_antes", "modalidade": "Particular"})
            elif "conv" in msg_lower:
                enviar_texto(phone, "Entendido! Qual o NOME do seu plano de saúde? (Ex: Amil, Bradesco, etc)")
                update_paciente(phone, {"status": "aguardando_nome_plano", "modalidade": "Convênio"})
            else:
                enviar_botoes(phone, "Por favor, utilize os botões:", ["💳 Convênio", "💎 Particular"])
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_nome_plano":
            plano = msg_recebida
            update_paciente(phone, {"convenio": plano})
            serv = info.get("servico", "")
            
            if serv == "Fisio Pélvica" and any(x in plano.lower() for x in ["amil", "bradesco"]):
                enviar_botoes(phone, f"O plano {plano} não cobre Fisioterapia Pélvica diretamente. Mas realizamos no Particular com recibo para reembolso. Deseja seguir no particular?", ["✅ Sim, Particular", "❌ Não, obrigado"])
                update_paciente(phone, {"status": "valida_reembolso"})
                return jsonify({"status": "ok"}), 200
                
            if is_veterano:
                 enviar_texto(phone, f"Plano {plano} registrado! ✅ Como você trocou de plano, envie uma FOTO ou PDF da sua NOVA CARTEIRINHA (frente). 📸")
                 update_paciente(phone, {"status": "aguardando_foto_carteirinha"})
            else:
                 enviar_texto(phone, f"Plano {plano} registrado! ✅ Para iniciarmos seu cadastro oficial, digite o seu CPF (apenas 11 números, sem traços):")
                 update_paciente(phone, {"status": "aguardando_cpf"})
            return jsonify({"status": "ok"}), 200

        elif status == "valida_reembolso":
            if "sim" in msg_lower:
                enviar_botoes(phone, "Excelente decisão! Qual o melhor período para a sua avaliação?", ["☀️ Manhã", "⛅ Tarde"])
                update_paciente(phone, {"status": "aguardando_periodo_particular_antes", "modalidade": "Particular"})
            else:
                enviar_texto(phone, "Tudo bem! Agradecemos o contato. Se mudar de ideia, estaremos aqui de portas abertas. ✨")
                update_paciente(phone, {"status": "finalizado"})
            return jsonify({"status": "ok"}), 200

        # CADASTRO SEQUENCIAL
        elif status == "aguardando_cpf":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) != 11:
                enviar_texto(phone, "⚠️ O CPF informado parece incorreto. Por favor, digite exatamente os 11 números, sem pontos ou espaços.")
                return jsonify({"status": "ok"}), 200
            
            update_paciente(phone, {"cpf": cpf_limpo})
            enviar_texto(phone, "CPF validado! ✅ Qual a sua Data de Nascimento? (Ex: 15/05/1980)")
            update_paciente(phone, {"status": "aguardando_nascimento"})
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_nascimento":
            update_paciente(phone, {"birthDate": msg_recebida})
            enviar_texto(phone, "Perfeito! Para finalizarmos seu cadastro, qual o seu melhor E-MAIL?")
            update_paciente(phone, {"status": "aguardando_email"})
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_email":
            update_paciente(phone, {"email": msg_recebida})
            mod = info.get("modalidade", "")
            
            if mod == "Convênio":
                enviar_texto(phone, "Cadastro concluído! 🎉\nAgora, vamos à documentação do plano. Por favor, envie uma FOTO NÍTIDA ou PDF da sua CARTEIRINHA (frente).")
                update_paciente(phone, {"status": "aguardando_foto_carteirinha"})
            elif mod == "Parceria App":
                enviar_texto(phone, "Cadastro concluído! 🎉\n\nPara validarmos o seu acesso, por favor, informe o seu **Wellhub ID** ou **Token do Totalpass** (você encontra no seu perfil do aplicativo):")
                update_paciente(phone, {"status": "aguardando_id_parceiro"})
            else:
                enviar_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para finalizar os detalhes e confirmar o seu horário. Aguarde um momento! 👩‍⚕️")
                update_paciente(phone, {"status": "finalizado"})
            return jsonify({"status": "ok"}), 200
            
        elif status == "aguardando_id_parceiro":
            update_paciente(phone, {"id_parceiro": msg_recebida})
            enviar_botoes(phone, "ID recebido! ✅ Para facilitar o seu dia a dia, como prefere agendar suas aulas de Pilates?", ["📱 App da Clínica", "🎫 App Parceiro"])
            update_paciente(phone, {"status": "pilates_app"})
            return jsonify({"status": "ok"}), 200
            
        elif status == "pilates_app":
            if "clínica" in msg_lower or "clinica" in msg_lower or "app" in msg_lower:
                msg = "Ótima escolha! 📲 Com o nosso App você tem total autonomia.\n\nBaixe o App **NextFit** na sua loja de aplicativos (Android ou iOS), faça um cadastro rápido e busque por: **Conectifisio - Ictus Fisioterapia SCS**.\n\nNossa equipe vai assumir agora para te liberar o acesso. Aguarde um instante! 👩‍⚕️"
            else:
                msg = "Perfeito! Você pode agendar suas aulas buscando nosso estúdio diretamente no seu app parceiro.\n\nNossa equipe assumirá o atendimento agora para confirmar seu primeiro check-in! 👩‍⚕️"
            enviar_texto(phone, msg)
            update_paciente(phone, {"status": "atendimento_humano", "queixa": f"[ACESSO APP PILATES]: {msg_recebida}"})
            return jsonify({"status": "ok"}), 200

        # ========================================================
        # 📄 FASE 6: DOCUMENTOS DE CONVÊNIO
        # ========================================================
        elif status == "aguardando_foto_carteirinha":
            if msg_type not in ['image', 'document']:
                enviar_texto(phone, "Por favor, utilize o botão de clipe (anexo) ou a câmera para nos enviar a FOTO ou o ARQUIVO (PDF) da carteirinha. 📸")
                return jsonify({"status": "ok"}), 200
            
            enviar_texto(phone, "Carteirinha recebida! ✅ Para finalizarmos a burocracia, envie a FOTO ou PDF DO SEU PEDIDO MÉDICO (que foi emitido há menos de 60 dias).")
            update_paciente(phone, {"status": "aguardando_foto_pedido"})
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_foto_pedido":
            if msg_type not in ['image', 'document']:
                enviar_texto(phone, "Por favor, nos envie a FOTO ou o ARQUIVO (PDF) do pedido médico. 📸")
                return jsonify({"status": "ok"}), 200
            
            enviar_botoes(phone, "Documentos recebidos com sucesso! 🎉 Para buscarmos vagas na nossa agenda de avaliações, qual o melhor período para você?", ["☀️ Manhã", "⛅ Tarde", "🌙 Noite"])
            update_paciente(phone, {"status": "aguardando_periodo"})
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_periodo":
            update_paciente(phone, {"periodo": msg_recebida, "status": "finalizado"})
            enviar_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para finalizar a integração com o plano e te confirmar o horário exato. Aguarde um instante! 👩‍⚕️")
            return jsonify({"status": "ok"}), 200

        # FALLBACK SAFETY
        enviar_texto(phone, "Nossa equipe já foi notificada e assumirá o seu atendimento em instantes. 👩‍⚕️")
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"❌ ERRO CRÍTICO NO CÓDIGO: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 500

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    return request.args.get("hub.challenge", "Acesso Negado"), 200

if __name__ == "__main__":
    app.run(port=5000)
