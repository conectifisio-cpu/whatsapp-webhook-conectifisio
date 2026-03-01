import os
import json
import traceback
import re
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

# ==========================================
# FUNÇÕES DE ESTADO DO PACIENTE
# ==========================================
def update_paciente(phone, data):
    if not db: return
    try:
        data["lastInteraction"] = firestore.SERVER_TIMESTAMP
        db.collection("PatientsKanban").document(phone).set(data, merge=True)
    except Exception as e:
        pass

# ==========================================
# 🔔 RECEPTOR DE WEBHOOKS (ALIMENTADOR DO ESPELHO)
# ==========================================
@app.route("/api/feegow-webhook", methods=["POST"])
def feegow_webhook():
    """Recebe o aviso do Feegow e guarda na nossa agenda espelho"""
    try:
        data = request.get_json()
        if not db or not data: return jsonify({"status": "ok"}), 200
        
        # 1. Guarda log bruto de segurança
        db.collection("FeegowWebhooksLog").add({
            "timestamp": firestore.SERVER_TIMESTAMP, 
            "payload": data
        })
        
        # 2. Cria o Espelho de Agendamentos Inteligente
        payload = data.get("payload", {})
        appt_id = payload.get("id")
        
        if appt_id:
            telefone_sujo = str(payload.get("telefone", ""))
            telefone_puro = re.sub(r'\D', '', telefone_sujo)
            
            db.collection("FeegowAppointments").document(str(appt_id)).set({
                "paciente_id": payload.get("PacienteID"),
                "data": payload.get("Data"),
                "hora": payload.get("Hora"),
                "status": payload.get("Status"),
                "telefone": telefone_puro,
                "paciente_nome": payload.get("NomePaciente"),
                "clinica": payload.get("NomeClinica"),
                "updatedAt": firestore.SERVER_TIMESTAMP
            }, merge=True)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"Erro Webhook: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 500

# ==========================================
# 🚀 MOTOR DE CONSULTA (LEITURA DO ESPELHO)
# ==========================================
def consultar_agenda_espelho(phone):
    """Lê a agenda direto do nosso Firebase corrigindo o Bug do 9º Dígito"""
    if not db: return [], "Banco de dados inacessível."
    
    # 1. Limpa o telefone que vem do WhatsApp (Seu celular pessoal via Meta)
    telefone_whatsapp = re.sub(r'\D', '', str(phone))
    if telefone_whatsapp.startswith("55") and len(telefone_whatsapp) > 11:
        telefone_whatsapp = telefone_whatsapp[2:] 
        
    # 2. A MÁGICA: Criamos sempre as duas versões para garantir o match perfeito
    if len(telefone_whatsapp) == 11:
        tel_com_9 = telefone_whatsapp
        tel_sem_9 = telefone_whatsapp[:2] + telefone_whatsapp[3:]
    else: # Se a Meta enviar com 10 dígitos (sem o 9)
        tel_sem_9 = telefone_whatsapp
        tel_com_9 = telefone_whatsapp[:2] + '9' + telefone_whatsapp[2:]

    sessoes = []
    
    try:
        # Busca nas duas variações matemáticas para não deixar margem de erro
        docs1 = list(db.collection("FeegowAppointments").where("telefone", "==", tel_com_9).stream())
        docs2 = list(db.collection("FeegowAppointments").where("telefone", "==", tel_sem_9).stream())
        
        # Junta os resultados sem duplicados
        all_docs_dict = {doc.id: doc for doc in docs1 + docs2}
        all_docs = list(all_docs_dict.values())
        
        hoje_str = datetime.now().strftime('%Y-%m-%d')
        
        for doc in all_docs:
            d = doc.to_dict()
            status = str(d.get("status", "")).lower()
            data_raw = str(d.get("data", ""))
            
            # Filtra consultas futuras
            if data_raw >= hoje_str and "cancelado" not in status and "falta" not in status:
                hora = str(d.get("hora", ""))[:5]
                dt_obj = datetime.strptime(data_raw, "%Y-%m-%d")
                sessoes.append(f"🗓️ *{dt_obj.strftime('%d/%m/%Y')} às {hora}* - {d.get('clinica', 'Conectifisio')}")

        sessoes = list(set(sessoes))
        sessoes.sort()
        
        if sessoes:
            return sessoes[:5], ""
        else:
            log_debug = f"Procurei por '{tel_com_9}' e '{tel_sem_9}', mas só encontrei 0 consultas válidas."
            return [], f"Não encontrei sessões futuras espelhadas no sistema.\n\n🔍 RAIO-X TÉCNICO:\n{log_debug}"
            
    except Exception as e:
        return [], f"Erro ao ler espelho: {str(e)}"

# ==========================================
# MENSAGERIA WHATSAPP
# ==========================================
def enviar_whatsapp(to, payload):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    try: requests.post(url, json={"messaging_product": "whatsapp", "to": to, **payload}, headers=headers, timeout=10)
    except: pass

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
        if "messages" not in value: return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]
        msg_recebida = message.get("text", {}).get("body", "").strip() or \
                       message.get("interactive", {}).get("button_reply", {}).get("title", "").strip()
        msg_lower = msg_recebida.lower()

        # 1. RESET
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

        # 3. COMANDO MÁGICO: REAGENDAR SESSÃO (AGORA USA O ESPELHO)
        if "reagendar sessão" in msg_lower or "agendamentos" in msg_lower:
            enviar_texto(phone, "Consultando nossos registros sincronizados... ⏳")
            sessoes_futuras, log_erro = consultar_agenda_espelho(phone)
            
            if sessoes_futuras:
                msg = f"✅ SUCESSO!\n\nLocalizei suas próximas sessões: 👇\n\n" + "\n".join(sessoes_futuras) + "\n\nQual delas gostaria de reagendar?"
                enviar_botoes(phone, msg, ["A Primeira", "Outra Data", "Falar com Recepção"])
            else:
                msg_falha = f"⚠️ {log_erro}\n\nDeseja agendar um novo horário com nossa equipe agora?"
                enviar_botoes(phone, msg_falha, ["☀️ Manhã", "⛅ Tarde", "⬅️ Voltar"])
            return jsonify({"status": "ok"}), 200

        # 4. MENU INICIAL
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
