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
# Lê o JSON seguro da Vercel sem expor a chave no código
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
if firebase_creds_json and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_creds_json)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        print(f"Erro ao carregar Firebase: {e}")

# Instância da Base de Dados
db = firestore.client() if firebase_admin._apps else None

# ==========================================
# FUNÇÕES DE MEMÓRIA (FIREBASE)
# ==========================================
def get_paciente(phone):
    """Busca a memória do paciente em 1 milissegundo"""
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
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"META API RESPONSE [{res.status_code}]: {res.text}") 
        return res
    except Exception as e:
        print(f"META API ERROR: {str(e)}")
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
        if msg_type == "text": msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type in ["image", "document"]:
            tem_anexo = True
            msg_recebida = "Anexo Recebido"

        # 1. COMANDOS DE RESET DE EMERGÊNCIA
        if msg_recebida.lower() in ["recomeçar", "reset", "menu inicial"]:
            update_paciente(phone, {"status": "triagem", "cellphone": phone})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            enviar_botoes(phone, "Atendimento reiniciado. 🔄\n\nEm qual unidade deseja ser atendido?", botoes)
            return jsonify({"status": "reset"}), 200

        # =====================================================
        # 2. CONSULTA ESTADO ATUAL (AGORA ULTRA-RÁPIDO NO FIREBASE)
        # =====================================================
        info = get_paciente(phone)
        
        # Cria registo básico se não existir
        if not info:
            info = {"cellphone": phone, "status": "triagem"}
            update_paciente(phone, info)

        status = info.get("status", "triagem")
        servico = info.get("servico", "")
        
        # Regra de Veterano: Se tem CPF e é válido (11 chars)
        cpf_salvo = info.get("cpf", "")
        is_veteran = True if len(re.sub(r'\D', '', cpf_salvo)) >= 11 else False
        
        # Super Trava: Garante que a modalidade nunca é esquecida
        modalidade = info.get("modalidade", "")
        convenio = info.get("convenio", "")
        if not modalidade and convenio: modalidade = "Convênio"
        elif not modalidade: modalidade = "Particular"

        # -----------------------------------------------------
        # O RETORNO INTELIGENTE PARA FLUXOS CONCLUÍDOS
        # -----------------------------------------------------
        if status in ["agendando", "finalizado"]:
            if is_veteran:
                update_paciente(phone, {"status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Reagendar"}, {"id": "v2", "title": "🔄 Retomar"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {info.get('title', 'paciente')}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", botoes)
                return jsonify({"status": "restart_veteran"}), 200
            else:
                status = "triagem"

        # -----------------------------------------------------
        # 3. MÁQUINA DE ESTADOS (TOTALMENTE INDEPENDENTE DO WIX)
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
            if msg_recebida in ["Recovery", "Liberação Miofascial"]:
                update_paciente(phone, {"servico": msg_recebida, "modalidade": "Particular", "status": "cadastrando_queixa"})
                responder_texto(phone, f"Ótima escolha para performance em {msg_recebida}! 🚀\n\nMe conte brevemente: o que te trouxe aqui hoje?")
            elif msg_recebida == "Fisio Neurológica":
                update_paciente(phone, {"servico": msg_recebida, "status": "triagem_neuro"})
                botoes = [{"id": "n1", "title": "🔹 Independente"}, {"id": "n2", "title": "🤝 Semidependente"}, {"id": "n3", "title": "👨‍🦽 Dependente"}]
                enviar_botoes(phone, "Para agendarmos com o especialista ideal, como está a mobilidade do paciente?\n\n🔹 *Independente:* Faz tudo sozinho.\n🤝 *Semidependente:* Precisa de apoio.\n👨‍🦽 *Dependente:* Auxílio constante.", botoes)
            else:
                update_paciente(phone, {"servico": msg_recebida, "status": "cadastrando_queixa"})
                responder_texto(phone, f"Entendido! {msg_recebida} selecionada.\n\nMe conte brevemente: o que te trouxe à clínica hoje?")

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
                # Na prática, a Vercel pode guardar o URL da imagem aqui, mas para simplificar registamos a receção
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
            update_paciente(phone, {"periodo": msg_recebida, "status": "finalizado"})
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
