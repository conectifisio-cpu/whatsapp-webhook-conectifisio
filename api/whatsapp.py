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
# INICIALIZAÇÃO FIREBASE (Com Diagnóstico)
# ==========================================
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
erro_firebase_init = None

if firebase_creds_json and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_creds_json, strict=False)
        if 'private_key' in cred_dict:
            cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        erro_firebase_init = f"Erro no JSON: {str(e)}"
        print(f"❌ ERRO FIREBASE INIT: {e}")
elif not firebase_creds_json:
    erro_firebase_init = "A variável FIREBASE_CREDENTIALS não foi encontrada na Vercel ou está vazia."

db = firestore.client() if firebase_admin._apps else None

# ==========================================
# FUNÇÕES DE BANCO DE DADOS
# ==========================================
def update_paciente(phone, data):
    if not db: return "Erro: Banco offline"
    try:
        data["lastInteraction"] = firestore.SERVER_TIMESTAMP
        db.collection("PatientsKanban").document(phone).set(data, merge=True)
        return "OK"
    except Exception as e:
        print(f"Erro Firebase Save: {e}")
        return str(e)

# ==========================================
# MENSAGERIA E COMPORTAMENTO HUMANO
# ==========================================
def simular_digitacao():
    time.sleep(1.5)

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

def enviar_texto(to, texto):
    simular_digitacao()
    enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

# ==========================================
# ESPELHO FEEGOW (WEBHOOKS E CONSULTA)
# ==========================================
@app.route("/api/feegow-webhook", methods=["POST"])
def feegow_webhook():
    try:
        data = request.get_json()
        if not db or not data: return jsonify({"status": "ok"}), 200
        db.collection("FeegowWebhooksLog").add({"timestamp": firestore.SERVER_TIMESTAMP, "payload": data})
        payload = data.get("payload", {})
        appt_id = payload.get("id")
        if appt_id:
            telefone_puro = re.sub(r'\D', '', str(payload.get("telefone", "")))
            proc_nome = payload.get("ProcedimentoNome") or payload.get("NomeEspecialidade") or ""
            db.collection("FeegowAppointments").document(str(appt_id)).set({
                "paciente_id": payload.get("PacienteID"),
                "data": payload.get("Data"),
                "hora": payload.get("Hora"),
                "status": payload.get("Status"),
                "telefone": telefone_puro,
                "paciente_nome": payload.get("NomePaciente"),
                "clinica": payload.get("NomeClinica"),
                "procedimento": proc_nome,
                "updatedAt": firestore.SERVER_TIMESTAMP
            }, merge=True)
        return jsonify({"status": "success"}), 200
    except: return jsonify({"status": "error"}), 500

def consultar_agenda_espelho(phone, servico_escolhido="Sessão"):
    if not db: return [], "Banco de dados inacessível."
    telefone_whatsapp = re.sub(r'\D', '', str(phone))
    if telefone_whatsapp.startswith("55") and len(telefone_whatsapp) > 11:
        telefone_whatsapp = telefone_whatsapp[2:] 
    tel_com_9 = telefone_whatsapp if len(telefone_whatsapp) == 11 else telefone_whatsapp[:2] + '9' + telefone_whatsapp[2:]
    tel_sem_9 = telefone_whatsapp[:2] + telefone_whatsapp[3:] if len(telefone_whatsapp) == 11 else telefone_whatsapp
    sessoes = []
    try:
        docs1 = list(db.collection("FeegowAppointments").where("telefone", "==", tel_com_9).stream())
        docs2 = list(db.collection("FeegowAppointments").where("telefone", "==", tel_sem_9).stream())
        all_docs = list({doc.id: doc for doc in docs1 + docs2}.values())
        hoje_str = datetime.now().strftime('%Y-%m-%d')
        for doc in all_docs:
            d = doc.to_dict()
            status = str(d.get("status", "")).lower()
            data_raw = str(d.get("data", ""))
            if data_raw >= hoje_str and "cancelado" not in status and "falta" not in status:
                hora = str(d.get("hora", ""))[:5]
                dt_obj = datetime.strptime(data_raw, "%Y-%m-%d")
                proc = d.get("procedimento") or servico_escolhido
                clinica = str(d.get('clinica', ''))
                unidade = "SCS" if "SCS" in clinica.upper() else ("Ipiranga" if "IPIRANGA" in clinica.upper() else "Conectifisio")
                sessoes.append(f"🗓️ *{dt_obj.strftime('%d/%m/%Y')} às {hora}* - {proc} ({unidade})")
        sessoes = list(set(sessoes))
        sessoes.sort()
        return sessoes[:3], "" if sessoes else "Não encontrei sessões futuras."
    except Exception as e: return [], f"Erro ao ler espelho: {str(e)}"

# ==========================================
# CÉREBRO MASTER (MÁQUINA DE ESTADOS)
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
        msg_recebida = message.get("text", {}).get("body", "") if msg_type == "text" else message.get("interactive", {}).get("button_reply", {}).get("title", "")
        msg_lower = msg_recebida.lower().strip()

        # -----------------------------------------------------
        # 🚨 SISTEMA DE DIAGNÓSTICO FIREBASE (RAIO-X)
        # -----------------------------------------------------
        if not db:
            enviar_texto(phone, f"🚨 *SISTEMA DE EMERGÊNCIA ATIVADO*\n\nO robô está funcionando perfeitamente, mas a ligação ao Firebase FALHOU.\n\n*Motivo Técnico:*\n{erro_firebase_init}\n\n*O que fazer:* Volte à Vercel e confirme se copiou o JSON inteiro para a variável FIREBASE_CREDENTIALS.")
            return jsonify({"status": "ok"}), 200
            
        if msg_lower == "raio-x firebase":
            resultado_salvar = update_paciente(phone, {"teste_diagnostico": "sucesso"})
            enviar_texto(phone, f"🔍 *DIAGNÓSTICO FIREBASE V151*\n\nConexão BD: ✅ OK\nTeste de Escrita: {resultado_salvar}\n\nSe o teste diz 'OK', os dados estão a ser gravados! Se o seu painel Firebase continua vazio, você provavelmente está a olhar para o projeto errado no site do Google.")
            return jsonify({"status": "ok"}), 200

        # LER MEMÓRIA DO FIREBASE
        info = {}
        try:
            doc_ref = db.collection("PatientsKanban").document(phone)
            doc = doc_ref.get()
            info = doc.to_dict() if doc.exists else {}
        except Exception as e:
            enviar_texto(phone, f"🚨 *ERRO DE LEITURA FIREBASE:*\n{str(e)}")
            return jsonify({"status": "ok"}), 200

        # -----------------------------------------------------
        # 🛡️ ESCUDOS GLOBAIS
        # -----------------------------------------------------
        if msg_type == "audio":
            enviar_texto(phone, "Ainda não consigo ouvir áudios por aqui 🎧.\n\nPara que eu possa te ajudar agora, por favor use os botões ou digite sua resposta.")
            return jsonify({"status": "ok"}), 200
            
        if msg_type in ["system", "unsupported", "unknown"]:
            enviar_texto(phone, "No momento, não conseguimos atender chamadas por este canal 📞. Por favor, nos envie uma mensagem de texto!")
            return jsonify({"status": "ok"}), 200

        status = info.get("status", "inicio")

        # COMANDO DE RESET
        if msg_lower in ["reset", "recomeçar", "menu inicial"]:
            if db: doc_ref.delete()
            enviar_botoes(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio. Para iniciarmos o seu atendimento, qual a unidade de preferência?", ["📍 Unidade SCS", "📍 Unid. Ipiranga"])
            update_paciente(phone, {"status": "triagem_unidade", "cellphone": phone})
            return jsonify({"status": "ok"}), 200

        # ESCUDO ANTI-LIXO
        if status not in ["inicio", "triagem_unidade", "aguardando_nome", "menu_veterano", "finalizado", "atendimento_humano"]:
            if msg_lower in ["oi", "olá", "ola", "bom dia", "boa tarde", "tudo bem"]:
                enviar_botoes(phone, "Olá! ✨ Notei que estávamos no meio do seu atendimento. Podemos continuar de onde paramos?", ["✅ Sim, continuar", "🔄 Recomeçar do zero"])
                return jsonify({"status": "ok"}), 200
                
            if msg_lower == "✅ sim, continuar":
                enviar_texto(phone, "Perfeito! Por favor, responda à nossa última pergunta para avançarmos.")
                return jsonify({"status": "ok"}), 200
            elif msg_lower == "🔄 recomeçar do zero":
                if db: doc_ref.delete()
                enviar_botoes(phone, "Tudo bem! Vamos recomeçar. De qual unidade você deseja atendimento?", ["📍 Unidade SCS", "📍 Unid. Ipiranga"])
                update_paciente(phone, {"status": "triagem_unidade"})
                return jsonify({"status": "ok"}), 200

        # -----------------------------------------------------
        # 🚪 FASES DO FLUXO
        # -----------------------------------------------------
        if status == "inicio":
            enviar_botoes(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio. Para iniciarmos o seu atendimento, qual a unidade de preferência?", ["📍 Unidade SCS", "📍 Unid. Ipiranga"])
            update_paciente(phone, {"status": "triagem_unidade", "cellphone": phone})
            return jsonify({"status": "ok"}), 200

        elif status == "triagem_unidade":
            unit = "Ipiranga" if "ipiranga" in msg_lower else "SCS"
            enviar_texto(phone, f"Unidade {unit} selecionada! ✅\n\nPara começarmos, como gostaria de ser chamado(a)?")
            update_paciente(phone, {"status": "aguardando_nome", "unit": unit})
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_nome":
            nome = msg_recebida.title()
            cpf_banco = info.get("cpf", "")
            is_veterano = len(re.sub(r'\D', '', cpf_banco)) == 11

            if is_veterano:
                enviar_botoes(phone, f"Olá, {nome}! ✨ Que bom ter você de volta na Conectifisio. Em que posso te ajudar hoje?", ["🗓️ Reagendar", "🔄 Nova Guia", "➕ Novo Serviço"])
                update_paciente(phone, {"status": "menu_veterano", "title": nome})
            else:
                msg = f"Prazer em conhecer, {nome}! 😊 Qual serviço você procura hoje?\n\n1️⃣ Fisio Ortopédica\n2️⃣ Fisio Neurológica\n3️⃣ Fisio Pélvica\n4️⃣ Acupuntura\n5️⃣ Pilates Studio\n6️⃣ Recovery / Liberação\n*(Digite o número ou nome)*"
                enviar_texto(phone, msg)
                update_paciente(phone, {"status": "aguardando_servico", "title": nome})
            return jsonify({"status": "ok"}), 200

        # --- A. FLUXO DO VETERANO ---
        elif status == "menu_veterano":
            if "reagendar" in msg_lower:
                enviar_texto(phone, "Consultando nossa agenda sincronizada... Um instante! ⏳")
                sessoes, erro = consultar_agenda_espelho(phone, info.get("servico", "Sessão"))
                if sessoes:
                    msg_agenda = "✅ Localizei suas próximas sessões: 👇\n\n" + "\n".join(sessoes) + "\n\nQual destas sessões você gostaria de reagendar?"
                    enviar_botoes(phone, msg_agenda, ["A Primeira", "Outra Data", "Falar c/ Recepção"])
                else:
                    enviar_botoes(phone, "Não encontrei sessões futuras marcadas. Mas não se preocupe, vamos agendar uma agora! Qual o melhor período para você?", ["☀️ Manhã", "⛅ Tarde"])
                update_paciente(phone, {"status": "aguardando_data_remarcacao"})
            elif "nova guia" in msg_lower:
                enviar_botoes(phone, "Excelente que vai continuar o tratamento! As novas sessões serão pelo seu CONVÊNIO ou PARTICULAR?", ["💳 Convênio", "💎 Particular"])
                update_paciente(phone, {"status": "veterano_modalidade"})
            elif "novo serviço" in msg_lower:
                msg = "Qual novo serviço deseja?\n\n1️⃣ Fisio Ortopédica\n2️⃣ Fisio Neurológica\n3️⃣ Fisio Pélvica\n4️⃣ Acupuntura\n5️⃣ Pilates Studio\n6️⃣ Recovery / Liberação"
                enviar_texto(phone, msg)
                update_paciente(phone, {"status": "aguardando_servico"})
            else:
                enviar_botoes(phone, "Por favor, escolha uma opção:", ["🗓️ Reagendar", "🔄 Nova Guia", "➕ Novo Serviço"])
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_data_remarcacao":
            if msg_lower in ["a primeira", "outra data"]:
                enviar_texto(phone, "Entendido! 🗓️ Para qual data e período (Manhã/Tarde) você gostaria de mudar a sua sessão?")
                return jsonify({"status": "ok"}), 200 
            enviar_texto(phone, "Recebido com sucesso! ✅\n\nNossa equipe vai confirmar essa alteração no sistema e te envia a confirmação final por aqui em instantes. Até já! 👩‍⚕️")
            update_paciente(phone, {"status": "atendimento_humano", "queixa": f"[REMARCAÇÃO PEDIDA]: {msg_recebida}"})
            return jsonify({"status": "ok"}), 200

        elif status == "veterano_modalidade":
            if "particular" in msg_lower:
                enviar_botoes(phone, "Ótimo! Como você já é nosso paciente, vamos direto para a agenda. Qual período você prefere para as sessões?", ["☀️ Manhã", "⛅ Tarde", "🌙 Noite"])
                update_paciente(phone, {"status": "aguardando_periodo", "modalidade": "Particular"})
            else:
                enviar_botoes(phone, "Você continua utilizando o MESMO CONVÊNIO ou houve alguma mudança no seu plano de saúde?", ["✅ Mesmo Convênio", "🔄 Troquei de Plano"])
                update_paciente(phone, {"status": "veterano_check_plano", "modalidade": "Convênio"})
            return jsonify({"status": "ok"}), 200

        elif status == "veterano_check_plano":
            if "troquei" in msg_lower:
                enviar_texto(phone, "Entendido! Vamos atualizar o sistema. Qual o NOME do seu NOVO convênio?")
                update_paciente(phone, {"status": "aguardando_nome_plano"})
            else:
                enviar_texto(phone, "Perfeito! Você já está com o NOVO PEDIDO MÉDICO em mãos? Se sim, por favor envie uma FOTO NÍTIDA dele agora. 📸")
                update_paciente(phone, {"status": "aguardando_foto_pedido"})
            return jsonify({"status": "ok"}), 200

        # --- B. FLUXO DO NOVO PACIENTE (E NOVO SERVIÇO) ---
        elif status == "aguardando_servico":
            servico = ""
            if "ortop" in msg_lower or "1" in msg_lower: servico = "Fisio Ortopédica"
            elif "neuro" in msg_lower or "2" in msg_lower: servico = "Fisio Neurológica"
            elif "pélvic" in msg_lower or "pelvic" in msg_lower or "3" in msg_lower: servico = "Fisio Pélvica"
            elif "acupuntura" in msg_lower or "4" in msg_lower: servico = "Acupuntura"
            elif "pilates" in msg_lower or "5" in msg_lower: servico = "Pilates Studio"
            elif "recovery" in msg_lower or "libera" in msg_lower or "6" in msg_lower: servico = "Recovery / Liberação"
            
            if not servico:
                enviar_texto(phone, "Por favor, digite o número correspondente (1 a 6) ou o nome do serviço.")
                return jsonify({"status": "ok"}), 200

            update_paciente(phone, {"servico": servico})

            if servico == "Pilates Studio":
                if info.get("unit") == "Ipiranga":
                    enviar_botoes(phone, "O Pilates Studio é uma modalidade exclusiva da unidade SCS. 🧘‍♀️ Deseja transferir o seu atendimento para lá?", ["Sim, mudar p/ SCS", "Não, escolher outro"])
                    update_paciente(phone, {"status": "pilates_transferencia"})
                else:
                    enviar_botoes(phone, "Excelente escolha! 🧘‍♀️ Como você pretende realizar as aulas?", ["💎 Plano Particular", "🏦 Saúde Caixa", "💪 Wellhub/Totalpass"])
                    update_paciente(phone, {"status": "pilates_modalidade"})
                return jsonify({"status": "ok"}), 200
            elif servico == "Fisio Neurológica":
                enviar_botoes(phone, "Para o especialista ideal, como está a mobilidade do paciente?", ["🔹 Independente", "🤝 Semidependente", "👨‍🦽 Dependente"])
                update_paciente(phone, {"status": "triagem_neuro"})
                return jsonify({"status": "ok"}), 200
            elif servico == "Recovery / Liberação":
                enviar_texto(phone, "Ótima escolha! Nossos serviços de alta performance são realizados de forma PARTICULAR. Para iniciarmos seu cadastro, digite seu CPF (apenas 11 números):")
                update_paciente(phone, {"status": "aguardando_cpf", "modalidade": "Particular"})
                return jsonify({"status": "ok"}), 200
            else:
                enviar_texto(phone, "Entendido! Me conte brevemente: o que te trouxe à clínica hoje? (Ex: dor lombar, pós-cirúrgico...)")
                update_paciente(phone, {"status": "aguardando_queixa"})
                return jsonify({"status": "ok"}), 200

        elif status == "pilates_transferencia":
            if "sim" in msg_lower:
                enviar_botoes(phone, "Unidade alterada para SCS! ✅ Como você pretende realizar as aulas de Pilates?", ["💎 Plano Particular", "🏦 Saúde Caixa", "💪 Wellhub/Totalpass"])
                update_paciente(phone, {"status": "pilates_modalidade", "unit": "SCS"})
            else:
                enviar_texto(phone, "Sem problemas! Qual outro serviço você procura? (Ex: 1 para Ortopedia)")
                update_paciente(phone, {"status": "aguardando_servico"})
            return jsonify({"status": "ok"}), 200

        elif status == "pilates_modalidade":
            if "particular" in msg_lower:
                enviar_botoes(phone, "No nosso estúdio, você conta com fisioterapeutas altamente especializados! ✨ Gostaria de agendar uma AULA EXPERIMENTAL GRATUITA?", ["Sim, gostaria", "Não, quero começar"])
                update_paciente(phone, {"status": "pilates_experimental", "modalidade": "Particular"})
            elif "caixa" in msg_lower:
                enviar_texto(phone, "Entendido! 🏦 Para o plano Saúde Caixa, é obrigatório pedido médico indicando Pilates ou Fisioterapia. Para seguirmos o cadastro, digite seu CPF (apenas 11 números):")
                update_paciente(phone, {"status": "aguardando_cpf", "modalidade": "Convênio", "convenio": "Saúde Caixa"})
            else:
                enviar_texto(phone, "Perfeito! Aceitamos Wellhub e Totalpass. Para seu cadastro inicial, digite seu CPF (apenas 11 números):")
                update_paciente(phone, {"status": "aguardando_cpf", "modalidade": "Parceria App"})
            return jsonify({"status": "ok"}), 200

        elif status == "pilates_experimental":
            enviar_botoes(phone, "Excelente! Para agilizarmos a sua agenda, qual o melhor período para você?", ["☀️ Manhã", "⛅ Tarde", "🌙 Noite"])
            update_paciente(phone, {"status": "aguardando_periodo_particular_antes"})
            return jsonify({"status": "ok"}), 200
            
        elif status == "aguardando_periodo_particular_antes":
            update_paciente(phone, {"periodo": msg_recebida})
            enviar_texto(phone, f"Período registrado! ✅ Agora, para finalizarmos seu cadastro, digite seu CPF (apenas 11 números):")
            update_paciente(phone, {"status": "aguardando_cpf"})
            return jsonify({"status": "ok"}), 200

        elif status == "triagem_neuro":
            if "dependente" in msg_lower and "semi" not in msg_lower:
                enviar_texto(phone, "Entendido. Devido à complexidade e para garantirmos o melhor cuidado, nosso fisioterapeuta assumirá o seu atendimento agora. Aguarde um instante! 👨‍⚕️")
                update_paciente(phone, {"status": "atendimento_humano", "queixa_ia": "[ALERTA: PACIENTE DEPENDENTE NEURO]"})
                return jsonify({"status": "ok"}), 200
            else:
                enviar_texto(phone, "Perfeito! Me conte brevemente: qual a principal queixa ou diagnóstico do paciente?")
                update_paciente(phone, {"status": "aguardando_queixa"})
                return jsonify({"status": "ok"}), 200

        elif status == "aguardando_queixa":
            update_paciente(phone, {"queixa": msg_recebida})
            enviar_texto(phone, "Compreendo a situação, sinto muito pelo desconforto. Fique tranquilo que vamos avaliar o melhor tratamento para você voltar a se movimentar bem! 💙")
            simular_digitacao()
            enviar_botoes(phone, "Para seguirmos com o agendamento, deseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", ["💳 Convênio", "💎 Particular"])
            update_paciente(phone, {"status": "escolha_modalidade"})
            return jsonify({"status": "ok"}), 200

        elif status == "escolha_modalidade":
            if "particular" in msg_lower:
                enviar_botoes(phone, "Ótima escolha! Para agilizarmos, qual o melhor período para você?", ["☀️ Manhã", "⛅ Tarde", "🌙 Noite"])
                update_paciente(phone, {"status": "aguardando_periodo_particular_antes", "modalidade": "Particular"})
            else:
                enviar_texto(phone, "Entendido! Qual o NOME do seu plano de saúde? (Ex: Amil, Bradesco, etc)")
                update_paciente(phone, {"status": "aguardando_nome_plano", "modalidade": "Convênio"})
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_nome_plano":
            plano = msg_recebida
            update_paciente(phone, {"convenio": plano})
            serv = info.get("servico", "")
            if serv == "Fisio Pélvica" and any(x in plano.lower() for x in ["amil", "bradesco"]):
                enviar_botoes(phone, f"O plano {plano} não cobre Fisioterapia Pélvica diretamente. Mas realizamos no Particular com recibo para reembolso. Deseja seguir no particular?", ["✅ Sim, Particular", "❌ Não, obrigado"])
                update_paciente(phone, {"status": "valida_reembolso"})
                return jsonify({"status": "ok"}), 200
                
            enviar_texto(phone, "Plano registrado! ✅ Para iniciarmos seu cadastro oficial, digite o seu CPF (apenas 11 números):")
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
                enviar_texto(phone, "Cadastro concluído! 🎉\nAgora, precisamos das fotos para liberar seu plano. Envie uma FOTO NÍTIDA da sua CARTEIRINHA (frente). 📸")
                update_paciente(phone, {"status": "aguardando_foto_carteirinha"})
            elif mod == "Parceria App":
                enviar_botoes(phone, "Cadastro concluído! 🎉 Para facilitar seu dia a dia, como prefere agendar suas aulas de Pilates?", ["📱 App da Clínica", "🎫 App Parceiro"])
                update_paciente(phone, {"status": "pilates_app"})
            else:
                enviar_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para finalizar os detalhes e confirmar o seu horário. Aguarde um momento! 👩‍⚕️")
                update_paciente(phone, {"status": "finalizado"})
            return jsonify({"status": "ok"}), 200
            
        elif status == "pilates_app":
            if "clínica" in msg_lower or "clinica" in msg_lower:
                msg = "Ótima escolha! 📲 Com o nosso App você tem total autonomia.\n\nBaixe o App NextFit, faça um cadastro rápido e busque por: Conectifisio - Ictus Fisioterapia SCS.\n\nNossa equipe vai assumir agora para te liberar o acesso. Aguarde! 👩‍⚕️"
            else:
                msg = "Perfeito! Você pode agendar suas aulas buscando nosso estúdio diretamente no seu app parceiro.\n\nNossa equipe assumirá o atendimento agora para confirmar seu primeiro check-in! 👩‍⚕️"
            enviar_texto(phone, msg)
            update_paciente(phone, {"status": "atendimento_humano", "queixa": f"[ACESSO APP PILATES]: {msg_recebida}"})
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_foto_carteirinha":
            if msg_type != 'image':
                enviar_texto(phone, "Por favor, utilize o botão de clipe (anexo) ou a câmera do WhatsApp para nos enviar a FOTO da carteirinha. 📸")
                return jsonify({"status": "ok"}), 200
            enviar_texto(phone, "Carteirinha recebida! ✅ Agora, envie a FOTO DO SEU PEDIDO MÉDICO (que foi emitido há menos de 60 dias).")
            update_paciente(phone, {"status": "aguardando_foto_pedido"})
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_foto_pedido":
            if msg_type != 'image' and msg_type != 'document':
                enviar_texto(phone, "Por favor, nos envie a FOTO ou PDF do pedido médico. 📸")
                return jsonify({"status": "ok"}), 200
            enviar_botoes(phone, "Documentos recebidos com sucesso! 🎉 Para buscarmos vagas na agenda, qual o melhor período para você?", ["☀️ Manhã", "⛅ Tarde", "🌙 Noite"])
            update_paciente(phone, {"status": "aguardando_periodo"})
            return jsonify({"status": "ok"}), 200

        elif status == "aguardando_periodo":
            update_paciente(phone, {"periodo": msg_recebida})
            enviar_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para finalizar a integração com o plano e te confirmar o horário exato. Aguarde um instante! 👩‍⚕️")
            update_paciente(phone, {"status": "finalizado"})
            return jsonify({"status": "ok"}), 200

        enviar_texto(phone, "Nossa equipe já foi notificada e assumirá o seu atendimento em instantes. 👩‍⚕️")
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"❌ ERRO CRÍTICO NO CÓDIGO: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    return request.args.get("hub.challenge", "Acesso Negado"), 200

if __name__ == "__main__":
    app.run(port=5000)
