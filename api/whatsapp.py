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
# FEEGOW: RADAR DE PRECISÃO (FOCO NO ERRO 422)
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
    """Radar de Precisão: Usa os parâmetros exatos para evitar o Erro 422"""
    if not FEEGOW_TOKEN or not feegow_id: return [], "Erro: feegow_id não encontrado."
    
    # Armadura de Headers (Simulando navegador e aceitando apenas JSON)
    headers = {
        "x-access-token": FEEGOW_TOKEN,
        "Authorization": FEEGOW_TOKEN, # Alguns endpoints .br pedem este
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
    }
    
    hoje = datetime.now()
    d_start = hoje.strftime('%Y-%m-%d')
    
    log_debug = f"🔍 RADAR DE PRECISÃO\nID: {feegow_id}\n"
    
    # ROTA ALVO: A que deu sinal de vida (v1/agendamentos) mas agora com parâmetros de data
    try:
        # Testamos a rota em português que o Wix usava
        url_br = f"https://api.feegow.com.br/v1/agendamentos?paciente_id={feegow_id}&data_inicio={d_start}"
        r_br = requests.get(url_br, headers=headers, timeout=8)
        log_debug += f"BR (Agendamentos): {r_br.status_code}\n"
        if r_br.status_code == 200:
            res = processar_lista_agendas(r_br.json(), hoje)
            if res: return res[:3], ""
    except: pass

    # ROTA DE BACKUP: A rota de 'appoints' antiga, mas forçando a data de hoje para evitar o 422
    try:
        url_old = f"https://api.feegow.com/v1/api/appoints?paciente_id={feegow_id}&data={d_start}"
        r_old = requests.get(url_old, headers=headers, timeout=8)
        log_debug += f"OLD (Appoints): {r_old.status_code}\n"
        if r_old.status_code == 200:
            res = processar_lista_agendas(r_old.json(), hoje)
            if res: return res[:3], ""
    except: pass

    return [], log_debug

# ==========================================
# WEBHOOK POST PRINCIPAL
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

        if msg_recebida.lower() in ["reset", "recomeçar"]:
            update_paciente(phone, {"status": "menu_veterano"})
            return jsonify({"status": "reset"}), 200

        info = get_paciente(phone)
        if "Agendamentos" in msg_recebida:
            feegow_id = info.get("feegow_id")
            if not feegow_id:
                vet = buscar_veterano_feegow_celular(phone)
                feegow_id = vet["feegow_id"] if vet else None
            
            lista, log = buscar_agendamentos_futuros_com_debug(feegow_id)
            if lista:
                msg = f"Olá! Localizei suas próximas sessões: 👇\n\n" + "\n".join(lista)
                enviar_whatsapp(phone, {"type": "text", "text": {"body": msg}})
            else:
                msg_erro = f"Não encontrei agendamentos futuros.\n\n*{log}*\n\nDeseja marcar um novo?"
                enviar_whatsapp(phone, {"type": "text", "text": {"body": msg_erro}})
        else:
            payload = {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": "Olá! Como posso ajudar?"},
                    "action": {"buttons": [{"type": "reply", "reply": {"id": "v1", "title": "🗓️ Agendamentos"}}]}
                }
            }
            enviar_whatsapp(phone, payload)
        return jsonify({"status": "success"}), 200
    except: return jsonify({"status": "ok"}), 200

def enviar_whatsapp(to, payload):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    requests.post(url, json={"messaging_product": "whatsapp", "to": to, **payload}, timeout=10)

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro": return request.args.get("hub.challenge"), 200
    return "Acesso Negado", 403

if __name__ == "__main__":
    app.run(port=5000)
