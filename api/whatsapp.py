import os
import requests
import traceback
import re
import json
from datetime import datetime, timedelta
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
        print("✅ Firebase Inicializado!")
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
# FEEGOW: BUSCA DE AGENDAMENTOS (FIX 422)
# ==========================================
def buscar_veterano_feegow_celular(phone):
    if not FEEGOW_TOKEN: return None
    celular = re.sub(r'\D', '', phone)
    if celular.startswith("55") and len(celular) > 11: celular = celular[2:]
    headers = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
    url = f"https://api.feegow.com/v1/api/patient/list?celular={celular}"
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            dados = res.json()
            if dados.get("success") and dados.get("content"):
                p = dados["content"][0]
                return {"feegow_id": p.get("id") or p.get("paciente_id"), "title": p.get("nome", "Paciente")}
    except: pass
    return None

def processar_lista_agendas(dados, hoje):
    itens = dados.get("content") or dados.get("data") or []
    if not isinstance(itens, list): itens = [itens] if itens else []
    lista_final = []
    for a in itens:
        status = str(a.get("status_nome", a.get("status", ""))).lower()
        if "cancelado" not in status and "falta" not in status:
            data_raw = str(a.get("data", ""))
            if "T" in data_raw: data_raw = data_raw.split("T")[0]
            try:
                dt_obj = datetime.strptime(data_raw, "%Y-%m-%d")
                if dt_obj.date() >= hoje.date():
                    proc = "Sessão"
                    if isinstance(a.get("procedimento"), dict): proc = a.get("procedimento", {}).get("nome", "Sessão")
                    elif a.get("procedimento_nome"): proc = a.get("procedimento_nome")
                    hora = str(a.get('horario', a.get('hora', '')))[:5]
                    lista_final.append(f"🗓️ *{dt_obj.strftime('%d/%m/%Y')} às {hora}* - {proc}")
            except: pass
    return sorted(list(set(lista_final)))

def buscar_agendamentos_futuros_com_debug(feegow_id):
    if not FEEGOW_TOKEN or not feegow_id: return [], "Erro: ID ausente."
    
    headers = {
        "x-access-token": FEEGOW_TOKEN,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
    }
    
    hoje = datetime.now()
    d_start = hoje.strftime('%Y-%m-%d')
    d_end = (hoje + timedelta(days=60)).strftime('%Y-%m-%d')
    
    log_debug = f"🔍 RADAR v127\nID: {feegow_id}\n"
    
    # TENTATIVA 1: Rota search (GET) - Corrigindo parâmetros para evitar 403/422
    try:
        url1 = f"https://api.feegow.com/v1/api/appoints/search?paciente_id={feegow_id}&data_start={d_start}&data_end={d_end}"
        r1 = requests.get(url1, headers=headers, timeout=8)
        log_debug += f"R1: {r1.status_code}\n"
        if r1.status_code == 200:
            res = processar_lista_agendas(r1.json(), hoje)
            if res: return res[:3], ""
    except: pass

    # TENTATIVA 2: Rota direta (GET) - Corrigindo para evitar 422
    try:
        # Enviamos apenas o paciente_id e a data_start (o Feegow costuma aceitar melhor assim)
        url2 = f"https://api.feegow.com/v1/api/appoints?paciente_id={feegow_id}&data={d_start}"
        r2 = requests.get(url2, headers=headers, timeout=8)
        log_debug += f"R2: {r2.status_code}\n"
        if r2.status_code == 200:
            res = processar_lista_agendas(r2.json(), hoje)
            if res: return res[:3], ""
    except: pass

    return [], log_debug

# ==========================================
# MENSAGERIA WHATSAPP
# ==========================================
def enviar_whatsapp(to, payload):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    requests.post(url, json={"messaging_product": "whatsapp", "to": to, **payload}, timeout=10)

def enviar_botoes(to, texto, botoes):
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]}
        }
    }
    enviar_whatsapp(to, payload)

# ==========================================
# WEBHOOK PRINCIPAL
# ==========================================
@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return jsonify({"status": "ok"}), 200
        message = value["messages"][0]
        phone = message["from"]
        msg_recebida = message.get("text", {}).get("body", "").strip() or \
                       message.get("interactive", {}).get("button_reply", {}).get("title", "")

        # CORREÇÃO: O comando reset agora responde ao usuário!
        if msg_recebida.lower() in ["reset", "recomeçar"]:
            update_paciente(phone, {"status": "menu_veterano"})
            enviar_botoes(phone, "Atendimento reiniciado para teste de agenda! 👇", [
                {"id": "v1", "title": "🗓️ Agendamentos"}, 
                {"id": "v2", "title": "🔄 Nova Guia"}
            ])
            return jsonify({"status": "reset"}), 200

        info = get_paciente(phone)
        if not info:
            info = {"cellphone": phone, "status": "menu_veterano"}
            update_paciente(phone, info)

        if "Agendamentos" in msg_recebida:
            feegow_id = info.get("feegow_id", "2279") # Forçando o ID do Marcel para o teste
            lista, log = buscar_agendamentos_futuros_com_debug(feegow_id)
            
            if lista:
                msg = f"Olá! Localizei as suas próximas sessões: 👇\n\n" + "\n".join(lista)
                enviar_whatsapp(phone, {"type": "text", "text": {"body": msg}})
            else:
                msg_erro = f"Não encontrei agendamentos futuros.\n\n*{log}*\n\nDeseja marcar um novo?"
                enviar_botoes(phone, msg_erro, [{"id": "m", "title": "Manhã"}, {"id": "t", "title": "Tarde"}])
        else:
            enviar_botoes(phone, "Olá! Como posso ajudar?", [
                {"id": "v1", "title": "🗓️ Agendamentos"}, 
                {"id": "v2", "title": "🔄 Nova Guia"}
            ])
        return jsonify({"status": "success"}), 200
    except: 
        print(f"❌ Erro: {traceback.format_exc()}")
        return jsonify({"status": "ok"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro": return request.args.get("hub.challenge"), 200
    return "Acesso Negado", 403

if __name__ == "__main__":
    app.run(port=5000)
