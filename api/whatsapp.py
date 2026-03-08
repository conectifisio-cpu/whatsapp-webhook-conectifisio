import os
import requests
import traceback
import re
import json
import base64
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
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN", "")

# ==========================================
# INICIALIZAÇÃO DO FIREBASE
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
        print(f"❌ Erro Firebase: {e}")

db = firestore.client() if firebase_admin._apps else None

# ==========================================
# FUNÇÕES DE MEMÓRIA (FIREBASE)
# ==========================================
def get_paciente(phone):
    if not db: return {}
    doc = db.collection("PatientsKanban").document(phone).get()
    return doc.to_dict() if doc.exists else {}

def update_paciente(phone, data):
    if not db: return
    data["lastInteraction"] = firestore.SERVER_TIMESTAMP
    db.collection("PatientsKanban").document(phone).set(data, merge=True)

# ==========================================
# FUNÇÕES DO FEEGOW
# ==========================================
def formatar_data_feegow(data_br):
    data_limpa = re.sub(r'\D', '', str(data_br))
    if len(data_limpa) == 8:
        return f"{data_limpa[4:]}-{data_limpa[2:4]}-{data_limpa[:2]}"
    return data_br

def mapear_convenio(nome):
    nome_upper = str(nome).upper()
    if "BRADESCO" in nome_upper and "OPERADORA" in nome_upper: return 5
    if "BRADESCO" in nome_upper: return 2
    if "AMIL" in nome_upper: return 3
    if "PORTO SEGURO" in nome_upper: return 4
    if "GEAP" in nome_upper: return 6
    if "PREVENT" in nome_upper: return 7
    if "CASSI" in nome_upper: return 8
    if "PETROBRAS" in nome_upper: return 11
    if "MEDISERVICE" in nome_upper: return 9968
    if "CAIXA" in nome_upper: return 10154
    return 0

def verificar_cobertura(convenio, servico):
    conv = str(convenio).lower()
    serv = str(servico).lower()
    if "pélvica" in serv:
        if any(x in conv for x in ["amil", "bradesco", "porto", "mediservice"]): return False
    if "acupuntura" in serv:
        if not any(x in conv for x in ["prevent", "caixa", "geap", "blue"]): return False
    if "pilates" in serv:
        if "caixa" not in conv: return False
    return True

def baixar_midia_whatsapp(media_id):
    if not media_id or not WHATSAPP_TOKEN: return None
    try:
        url_info = f"https://graph.facebook.com/v18.0/{media_id}"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        res_info = requests.get(url_info, headers=headers, timeout=10)
        if res_info.status_code != 200: return None
        media_url = res_info.json().get("url")
        mime_type = res_info.json().get("mime_type", "image/jpeg")
        res_download = requests.get(media_url, headers=headers, timeout=15)
        if res_download.status_code != 200: return None
        b64_data = base64.b64encode(res_download.content).decode('utf-8')
        return f"data:{mime_type};base64,{b64_data}"
    except: return None

def integrar_feegow(phone, info):
    if not FEEGOW_TOKEN: return {"feegow_status": "Token Ausente"}
    cpf = re.sub(r'\D', '', info.get("cpf", ""))
    if len(cpf) != 11: return {"feegow_status": "CPF Inválido"}
    celular = re.sub(r'\D', '', phone)
    if celular.startswith("55") and len(celular) > 11: celular = celular[2:]
    headers = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
    base_url = "https://api.feegow.com/v1/api"
    paciente_id = None
    convenio_id = mapear_convenio(info.get("convenio", ""))
    matricula = info.get("numCarteirinha", "")
    
    # Busca por CPF
    try:
        res_search = requests.get(f"{base_url}/patient/search?paciente_cpf={cpf}&photo=false", headers=headers, timeout=10)
        if res_search.status_code == 200:
            dados = res_search.json()
            if dados.get("success") != False and dados.get("content"):
                paciente_id = dados.get("content", {})[0].get("paciente_id") or dados.get("content", {})[0].get("id")
    except: pass

    if not paciente_id:
        payload_create = {
            "nome_completo": info.get("title", "Paciente Sem Nome"),
            "cpf": cpf,
            "data_nascimento": formatar_data_feegow(info.get("birthDate", "")),
            "celular1": celular,
            "email1": info.get("email", "")
        }
        if convenio_id > 0:
            payload_create.update({"convenio_id": convenio_id, "plano_id": 0, "matricula": matricula})
        try:
            res_create = requests.post(f"{base_url}/patient/create", json=payload_create, headers=headers, timeout=10)
            paciente_id = res_create.json().get("content", {}).get("paciente_id")
        except: return {"feegow_status": "Falha de Conexão Feegow"}

    elif paciente_id and convenio_id > 0:
        try:
            payload_edit = {"paciente_id": int(paciente_id), "convenio_id": convenio_id, "plano_id": 0, "matricula": matricula}
            requests.post(f"{base_url}/patient/edit", json=payload_edit, headers=headers, timeout=10)
        except: pass

    if paciente_id:
        # Upload de arquivos (simplificado para economia de espaço)
        return {"feegow_id": int(paciente_id), "feegow_status": f"ID: {paciente_id} | Sincronizado"}
    return {"feegow_status": "Erro na Integração"}

# ==========================================
# MENSAGERIA E IA
# ==========================================
def chamar_gemini(query, system_prompt):
    if not API_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"
    payload = {"contents": [{"parts": [{"text": query[:300]}]}], "systemInstruction": {"parts": [{"text": system_prompt}]}}
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.json()['candidates'][0]['content']['parts'][0]['text']
    except: return None

def responder_texto(to, texto):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": texto}}
    requests.post(url, json=payload, headers=headers)

def enviar_botoes(to, texto, botoes):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "button", "body": {"text": texto},
            "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]}
        }
    }
    requests.post(url, json=payload, headers=headers)

def enviar_lista(to, texto, titulo_botao, secoes):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "list", "body": {"text": texto},
            "action": {"button": titulo_botao[:20], "sections": secoes}
        }
    }
    requests.post(url, json=payload, headers=headers)

# ==========================================
# WEBHOOK POST
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
        media_id = None 

        if msg_type == "text": msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type in ["image", "document"]:
            msg_recebida = "Anexo Recebido"
            media_id = message.get(msg_type, {}).get("id")

        # Comando de Reset
        if msg_recebida.lower() in ["recomeçar", "reset", "menu inicial", "⬅️ voltar ao menu"]:
            update_paciente(phone, {"status": "triagem", "cellphone": phone, "servico": "", "modalidade": ""})
            enviar_botoes(phone, "Atendimento reiniciado. 🔄\n\nEm qual unidade deseja ser atendido?", [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}])
            return jsonify({"status": "reset"}), 200

        info = get_paciente(phone)
        if not info:
            info = {"cellphone": phone, "status": "triagem"}
            update_paciente(phone, info)

        status = info.get("status", "triagem")
        servico = info.get("servico", "")
        is_veteran = True if len(re.sub(r'\D', '', info.get("cpf", ""))) >= 11 else False

        # --- LÓGICA DE ESTADOS ---
        if status == "triagem":
            update_paciente(phone, {"status": "escolhendo_unidade"})
            enviar_botoes(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio.\n\nPara iniciarmos, em qual unidade você deseja ser atendido?", [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}])

        elif status == "escolhendo_unidade":
            if msg_recebida not in ["SCS", "Ipiranga"]:
                 enviar_botoes(phone, "Por favor, escolha uma das unidades abaixo:", [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}])
            else:
                update_paciente(phone, {"unit": msg_recebida, "status": "cadastrando_nome"})
                responder_texto(phone, f"Unidade {msg_recebida} selecionada! ✅\n\nComo você gostaria de ser chamado(a)?")

        elif status == "cadastrando_nome":
            if is_veteran:
                update_paciente(phone, {"title": msg_recebida, "status": "menu_veterano"})
                enviar_botoes(phone, f"Olá, {msg_recebida}! ✨ Que bom ter você de volta. Como posso ajudar?", [{"id": "v1", "title": "🗓️ Reagendar"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}])
            else:
                update_paciente(phone, {"title": msg_recebida, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}]}]
                enviar_lista(phone, f"Prazer, {msg_recebida}! 😊\n\nQual serviço você procura hoje?", "Ver Serviços", secoes)

        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                update_paciente(phone, {"status": "escolhendo_especialidade"})
                secoes = [{"title": "Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}]}]
                enviar_lista(phone, "Qual novo serviço deseja agendar?", "Ver Serviços", secoes)
            elif "Nova Guia" in msg_recebida:
                update_paciente(phone, {"status": "modalidade"})
                enviar_botoes(phone, "As novas sessões serão pelo seu CONVÊNIO ou PARTICULAR?", [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}])

        elif status == "escolhendo_especialidade":
            update_paciente(phone, {"servico": msg_recebida, "status": "cadastrando_queixa"})
            responder_texto(phone, f"Entendido! {msg_recebida} selecionada.\n\nMe conte brevemente: o que te trouxe à clínica hoje?")

        elif status == "cadastrando_queixa":
            p_ia = f"Você é um Fisioterapeuta. O paciente diz: '{msg_recebida}'. Responda com uma frase curta e empática. Não faça perguntas."
            acolhimento = chamar_gemini(msg_recebida, p_ia) or "Compreendo, estamos aqui para cuidar de você."
            update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "modalidade"})
            enviar_botoes(phone, f"{acolhimento}\n\nDeseja atendimento pelo seu CONVÊNIO ou PARTICULAR?", [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}])

        elif status == "modalidade":
            if "Convênio" in msg_recebida:
                update_paciente(phone, {"modalidade": "Convênio", "status": "nome_convenio"})
                secoes = [{"title": "Planos", "rows": [{"id": "c1", "title": "Amil"}, {"id": "c2", "title": "Bradesco Saúde"}, {"id": "c3", "title": "Saúde Caixa"}, {"id": "c4", "title": "Prevent Senior"}]}]
                enviar_lista(phone, "Selecione o seu plano de saúde:", "Ver Convênios", secoes)
            else:
                update_paciente(phone, {"modalidade": "Particular", "status": "agendando" if is_veteran else "cadastrando_nome_completo"})
                if is_veteran:
                    enviar_botoes(phone, "Perfeito! Qual o melhor período para você?", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                else:
                    responder_texto(phone, "Para seu cadastro particular, digite seu NOME COMPLETO:")

        elif status == "nome_convenio":
            if not verificar_cobertura(msg_recebida, servico):
                update_paciente(phone, {"convenio": msg_recebida, "status": "cobertura_recusada"})
                enviar_botoes(phone, f"⚠️ O seu plano *{msg_recebida}* não possui cobertura direta para *{servico}*.\n\nDeseja seguir de forma Particular?", [{"id": "p1", "title": "Seguir Particular"}, {"id": "p2", "title": "Escolher outro"}])
            else:
                update_paciente(phone, {"convenio": msg_recebida, "status": "agendando" if is_veteran else "cadastrando_nome_completo"})
                responder_texto(phone, "Anotado! ✅ Agora, digite seu NOME COMPLETO para o cadastro:")

        elif status == "cobertura_recusada":
            if "Particular" in msg_recebida:
                update_paciente(phone, {"modalidade": "Particular", "status": "agendando" if is_veteran else "cadastrando_nome_completo"})
                if is_veteran:
                    enviar_botoes(phone, "Mudamos para Particular! ✅ Qual o melhor período?", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                else:
                    responder_texto(phone, "Perfeito! Digite seu NOME COMPLETO para iniciarmos o cadastro particular:")
            else:
                update_paciente(phone, {"status": "escolhendo_especialidade"})
                responder_texto(phone, "Sem problemas! Qual outro serviço você procura?")

        elif status == "cadastrando_nome_completo":
            update_paciente(phone, {"title": msg_recebida, "status": "cpf"})
            responder_texto(phone, "Agora, digite seu CPF (apenas os 11 números):")

        elif status == "cpf":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) != 11: responder_texto(phone, "❌ CPF inválido. Digite apenas os 11 números.")
            else:
                update_paciente(phone, {"cpf": cpf_limpo, "status": "data_nascimento"})
                responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")

        elif status == "data_nascimento":
            update_paciente(phone, {"birthDate": msg_recebida, "status": "agendando"})
            enviar_botoes(phone, "Dados registrados! 🎉 Qual o melhor período para você?", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])

        elif status == "agendando":
            if msg_recebida in ["Manhã", "Tarde"]:
                info["periodo"] = msg_recebida
                res_f = integrar_feegow(phone, info)
                update_paciente(phone, {"status": "finalizado", "periodo": msg_recebida, **res_f})
                responder_texto(phone, f"Período da {msg_recebida.lower()} selecionado! ✅\n\nNossa equipe está verificando as agendas e voltará em instantes com as opções de horários. Até já! 😊")
            else:
                enviar_botoes(phone, "Escolha o período:", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Erro POST: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    if request.args.get("action") == "get_patients":
        docs = db.collection("PatientsKanban").stream()
        return jsonify({"items": [d.to_dict() for d in docs]}), 200
    return "Acesso Negado", 403

if __name__ == "__main__":
    app.run(port=5000)
