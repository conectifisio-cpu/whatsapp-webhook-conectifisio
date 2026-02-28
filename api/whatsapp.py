import os
import requests
import traceback
import re
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN", "")

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
        print("✅ Firebase Inicializado com Sucesso!")
    except Exception as e:
        print(f"❌ ERRO FIREBASE INIT: {e}")

db = firestore.client() if firebase_admin._apps else None

# ==========================================
# FUNÇÃO DE SALVAMENTO FIREBASE
# ==========================================
def update_paciente(phone, data):
    if not db: 
        print("❌ ERRO: Tentou salvar no Firebase, mas o DB não está conectado.")
        return
    try:
        data["lastInteraction"] = firestore.SERVER_TIMESTAMP
        db.collection("PatientsKanban").document(phone).set(data, merge=True)
        print(f"✅ Dados salvos no Firebase para o número {phone}")
    except Exception as e:
        print(f"❌ ERRO AO SALVAR FIREBASE: {e}")

# ==========================================
# 🚀 MOTOR DE CONSULTA FEEGOW
# ==========================================
def consultar_agenda_feegow(cpf):
    if not FEEGOW_TOKEN or not cpf: return [], "CPF ou Token ausente."
    cpf_limpo = re.sub(r'\D', '', str(cpf))
    headers_br = {"Authorization": FEEGOW_TOKEN, "Content-Type": "application/json"}
    base_url = "https://api.feegow.com.br/v1"

    try:
        res_pac = requests.get(f"{base_url}/pacientes?cpf={cpf_limpo}", headers=headers_br, timeout=8)
        if res_pac.status_code != 200 or not res_pac.json().get("data"): return [], "Paciente não localizado."
        paciente_id = res_pac.json()["data"][0]["id"]
    except: return [], "Falha na comunicação com servidor da clínica."

    hoje = datetime.now().strftime("%Y-%m-%d")
    rotas = [
        f"{base_url}/appoints?paciente_id={paciente_id}&data_start={hoje}",
        f"{base_url}/agendamentos?paciente_id={paciente_id}",
        f"https://api.feegow.com/v1/api/appoints?paciente_id={paciente_id}&data={hoje}"
    ]

    for url in rotas:
        try:
            req_headers = {"x-access-token": FEEGOW_TOKEN} if "api.feegow.com/v1/api" in url else headers_br
            res = requests.get(url, headers=req_headers, timeout=8)
            if res.status_code == 200:
                dados = res.json()
                itens = dados.get("data") or dados.get("content") or []
                sessoes = []
                for a in itens:
                    status = str(a.get("status_nome", a.get("status", ""))).lower()
                    if "cancelado" not in status and "falta" not in status:
                        data_raw = str(a.get("data", "")).split("T")[0]
                        if data_raw >= hoje:
                            proc = a.get("procedimento_nome") or a.get("procedimento", {}).get("nome", "Sessão")
                            hora = str(a.get("horario", a.get("hora", "")))[:5]
                            dt_obj = datetime.strptime(data_raw, "%Y-%m-%d")
                            sessoes.append(f"🗓️ *{dt_obj.strftime('%d/%m/%Y')} às {hora}* - {proc}")
                if sessoes: return sessoes[:3], ""
        except: continue
    return [], ""

# ==========================================
# MENSAGERIA WHATSAPP (AGORA COM RADAR DE ERRO)
# ==========================================
def enviar_whatsapp(to, payload):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    try: 
        res = requests.post(url, json={"messaging_product": "whatsapp", "to": to, **payload}, headers=headers, timeout=10)
        # O RADAR: Imprime a resposta da Meta na Vercel!
        print(f"📩 META RESPONSE ({res.status_code}): {res.text}")
    except Exception as e: 
        print(f"❌ ERRO AO ENVIAR WHATSAPP: {e}")

def enviar_botoes(to, texto, botoes):
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
    enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

# ==========================================
# CÉREBRO PRINCIPAL
# ==========================================
@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        
        # Filtra os avisos de "mensagem lida/entregue" da Meta para não dar erro
        if "messages" not in value: 
            return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]
        msg_recebida = message.get("text", {}).get("body", "").strip() or \
                       message.get("interactive", {}).get("button_reply", {}).get("title", "").strip()
        msg_lower = msg_recebida.lower()

        print(f"💬 MENSAGEM RECEBIDA DE {phone}: {msg_recebida}")

        # 1. RESET ABSOLUTO
        if msg_lower in ["reset", "recomeçar", "menu inicial"]:
            if db: db.collection("PatientsKanban").document(phone).delete()
            enviar_botoes(phone, "Atendimento reiniciado! Como posso ajudar hoje?", ["🗓️ Reagendar Sessão", "➕ Novo Serviço"])
            return jsonify({"status": "ok"}), 200

        # 2. LER OU CRIAR MEMÓRIA NO FIREBASE
        info = {}
        if db:
            doc_ref = db.collection("PatientsKanban").document(phone)
            doc = doc_ref.get()
            if doc.exists:
                info = doc.to_dict()
                update_paciente(phone, {"lastInteraction": firestore.SERVER_TIMESTAMP})
            else:
                info = {"cellphone": phone, "status": "menu_veterano", "title": "Paciente Teste"}
                update_paciente(phone, info)

        # 3. CAPTURA DE CPF
        if info.get("status") == "aguardando_cpf_agenda":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) == 11:
                update_paciente(phone, {"cpf": cpf_limpo, "status": "menu_veterano"})
                info["cpf"] = cpf_limpo
                enviar_texto(phone, f"CPF registado! ✅")
                msg_lower = "reagendar sessão"
            else:
                enviar_texto(phone, "Por favor, digite um CPF válido com 11 números:")
                return jsonify({"status": "ok"}), 200

        # 4. COMANDO MÁGICO: REAGENDAR SESSÃO
        if "reagendar sessão" in msg_lower or "agendamentos" in msg_lower:
            cpf_paciente = info.get("cpf")
            if not cpf_paciente:
                enviar_texto(phone, "Para consultar o sistema, por favor, digite o seu CPF (11 números):")
                update_paciente(phone, {"status": "aguardando_cpf_agenda"})
                return jsonify({"status": "ok"}), 200
            
            enviar_texto(phone, "Estou consultando a sua agenda diretamente no sistema da clínica... um instante. ⏳")
            sessoes, erro = consultar_agenda_feegow(cpf_paciente)
            
            if sessoes:
                msg = "Localizei suas próximas sessões: 👇\n\n" + "\n".join(sessoes) + "\n\nQual delas você gostaria de reagendar?"
                enviar_botoes(phone, msg, ["A Primeira", "Outra Data", "Falar com Recepção"])
            else:
                msg_falha = "Não encontrei agendamentos futuros no sistema."
                if erro: msg_falha += f" ({erro})"
                msg_falha += "\n\nMas não se preocupe, vamos agendar agora! Qual o melhor período?"
                enviar_botoes(phone, msg_falha, ["☀️ Manhã", "⛅ Tarde", "⬅️ Voltar"])
            return jsonify({"status": "ok"}), 200

        # 5. MENU INICIAL VETERANO SIMULADO
        enviar_botoes(phone, f"Olá! ✨ Que bom ter você de volta na Conectifisio. Como posso ajudar?", ["🗓️ Reagendar Sessão", "🔄 Nova Guia", "➕ Novo Serviço"])
        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ ERRO CRÍTICO NO CÓDIGO: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    return request.args.get("hub.challenge", "Acesso Negado"), 200

if __name__ == "__main__":
    app.run(port=5000)
