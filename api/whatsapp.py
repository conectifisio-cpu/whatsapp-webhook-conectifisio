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
# Estas variáveis devem ser configuradas no painel da Vercel
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
    """Recupera os dados do paciente do Firestore usando o telefone como ID"""
    if not db: return {}
    doc = db.collection("PatientsKanban").document(phone).get()
    return doc.to_dict() if doc.exists else {}

def update_paciente(phone, data):
    """Atualiza ou cria o registro do paciente no Firestore"""
    if not db: return
    data["lastInteraction"] = firestore.SERVER_TIMESTAMP
    db.collection("PatientsKanban").document(phone).set(data, merge=True)

# ==========================================
# 🔔 RECEPTOR DE WEBHOOKS (ESCUTA PASSIVA)
# ==========================================
@app.route("/api/feegow-webhook", methods=["POST"])
def feegow_webhook():
    """Porta de entrada para avisos automáticos vindos do Feegow"""
    try:
        data = request.get_json()
        if db and data:
            # Regista o log para análise posterior da estrutura
            db.collection("FeegowWebhooksLog").add({
                "timestamp": firestore.SERVER_TIMESTAMP, 
                "payload": data
            })
        return jsonify({"status": "success"}), 200
    except:
        return jsonify({"status": "error"}), 500

# ==========================================
# FEEGOW: SUPER RADAR DE BUSCA DE AGENDAMENTOS
# ==========================================
def buscar_veterano_feegow_celular(phone):
    """Tenta localizar o paciente no Feegow usando o número do telemóvel"""
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
                return {
                    "feegow_id": p.get("id") or p.get("paciente_id"), 
                    "title": p.get("nome", "Paciente"), 
                    "cpf": re.sub(r'\D', '', str(p.get("cpf", "")))
                }
    except: pass
    return None

def processar_lista_agendas(dados, hoje):
    """Filtra e formata os agendamentos das diversas APIs do Feegow"""
    itens = dados.get("content") or dados.get("data") or []
    if not isinstance(itens, list): itens = [itens] if itens else []
    
    lista_final = []
    for a in itens:
        # Ignora sessões canceladas ou faltas
        status = str(a.get("status_nome", a.get("status", ""))).lower()
        if "cancelado" not in status and "falta" not in status:
            data_raw = str(a.get("data", ""))
            if "T" in data_raw: data_raw = data_raw.split("T")[0]
            try:
                dt_obj = datetime.strptime(data_raw, "%Y-%m-%d")
                # Apenas datas de hoje em diante
                if dt_obj.date() >= hoje.date():
                    # Identifica o nome do procedimento (vários formatos de API)
                    proc = "Sessão"
                    if isinstance(a.get("procedimento"), dict): 
                        proc = a.get("procedimento", {}).get("nome", "Sessão")
                    elif a.get("procedimento_nome"): 
                        proc = a.get("procedimento_nome")
                    
                    hora = str(a.get('horario', a.get('hora', '')))[:5]
                    item = f"🗓️ *{dt_obj.strftime('%d/%m/%Y')} às {hora}* - {proc}"
                    if item not in lista_final: lista_final.append(item)
            except: pass
    
    # Ordena por data mais próxima
    return sorted(lista_final)

def buscar_agendamentos_futuros_com_debug(feegow_id):
    """
    SUPER RADAR: Tenta 3 rotas diferentes para localizar sessões.
    Ideal para encontrar tanto sessões de 'Equipamento Alocado' quanto da 'Agenda Diária'.
    """
    if not FEEGOW_TOKEN or not feegow_id: 
        return [], "🔍 DEBUG: feegow_id não encontrado para este paciente."
    
    headers = {
        "x-access-token": FEEGOW_TOKEN,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
    }
    
    hoje = datetime.now()
    futuro = hoje + timedelta(days=60)
    d_start = hoje.strftime('%Y-%m-%d')
    d_end = futuro.strftime('%Y-%m-%d')
    
    log_debug = f"🔍 SUPER RADAR ATIVO\nID Paciente: {feegow_id}\n"
    
    # ROTA 1: Pesquisa Oficial (Busca Agendas Diárias Clínicas)
    try:
        url1 = f"https://api.feegow.com/v1/api/appoints/search?paciente_id={feegow_id}&data_start={d_start}&data_end={d_end}"
        r1 = requests.get(url1, headers=headers, timeout=8)
        log_debug += f"R1 (Search): {r1.status_code}\n"
        if r1.status_code == 200:
            res = processar_lista_agendas(r1.json(), hoje)
            if res: return res[:3], ""
    except: pass

    # ROTA 2: Lista Geral de Agendamentos (Frequente para Equipamentos/Sessões Seriadas)
    try:
        url2 = f"https://api.feegow.com/v1/api/appoints?paciente_id={feegow_id}"
        r2 = requests.get(url2, headers=headers, timeout=8)
        log_debug += f"R2 (Geral): {r2.status_code}\n"
        if r2.status_code == 200:
            res = processar_lista_agendas(r2.json(), hoje)
            if res: return res[:3], ""
    except: pass

    # ROTA 3: Endpoint de Agendamentos por ID de Paciente (Nova API)
    try:
        url3 = f"https://api.feegow.com.br/v1/patient/{feegow_id}/appoints"
        h3 = headers.copy()
        h3["Authorization"] = FEEGOW_TOKEN
        r3 = requests.get(url3, headers=h3, timeout=8)
        log_debug += f"R3 (Relacional): {r3.status_code}\n"
        if r3.status_code == 200:
            res = processar_lista_agendas(r3.json(), hoje)
            if res: return res[:3], ""
    except: pass

    return [], log_debug

# ==========================================
# MENSAGERIA WHATSAPP
# ==========================================
def enviar_whatsapp(to, payload_msg):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    try: requests.post(url, json=payload, headers=headers, timeout=10)
    except: pass

def enviar_botoes(to, texto, botoes):
    """Envia botões interativos para facilitar a vida do paciente"""
    payload = {
        "type": "interactive", 
        "interactive": {
            "type": "button", 
            "body": {"text": texto}, 
            "action": {
                "buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]
            }
        }
    }
    enviar_whatsapp(to, payload)

# ==========================================
# WEBHOOK POST PRINCIPAL (ROTA DO WHATSAPP)
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
        if msg_type == "text": 
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            msg_recebida = message["interactive"].get("button_reply", {}).get("title", "")

        # Comando de Reset
        if msg_recebida.lower() in ["reset", "recomeçar", "menu"]:
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

        # FLUXO DE BUSCA DE AGENDA (O FOCO DO TESTE)
        if "Agendamentos" in msg_recebida:
            feegow_id = info.get("feegow_id")
            
            # Se não tem ID, tenta resgatar pelo telefone
            if not feegow_id:
                vet = buscar_veterano_feegow_celular(phone)
                if vet: 
                    feegow_id = vet["feegow_id"]
                    update_paciente(phone, {"feegow_id": feegow_id, "title": vet["title"]})

            # DISPARA O SUPER RADAR
            lista, log = buscar_agendamentos_futuros_com_debug(feegow_id)
            
            if lista:
                msg = f"Olá, {info.get('title', 'Paciente')}! Localizei as suas próximas sessões: 👇\n\n" + "\n".join(lista)
                enviar_botoes(phone, msg, [
                    {"id": "ok", "title": "👍 Consultar outro"}, 
                    {"id": "ajuda", "title": "👤 Falar com Recepção"}
                ])
            else:
                msg_erro = "Não encontrei agendamentos futuros para você no sistema. 🤔\n"
                if log: msg_erro += f"\n*{log}*"
                msg_erro += "\n\nPosso agendar um novo agora! Qual o melhor período para você?"
                enviar_botoes(phone, msg_erro, [
                    {"id": "m", "title": "Manhã"}, 
                    {"id": "t", "title": "Tarde"}
                ])
        else:
            # Menu Inicial Padrão do Teste
            enviar_botoes(phone, "Olá! Como posso ajudar hoje?", [
                {"id": "v1", "title": "🗓️ Agendamentos"}, 
                {"id": "v2", "title": "🔄 Nova Guia"}
            ])

        return jsonify({"status": "success"}), 200
    except:
        print(f"❌ Erro Crítico: {traceback.format_exc()}")
        return jsonify({"status": "ok"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro": 
        return request.args.get("hub.challenge"), 200
    return "Acesso Negado", 403

if __name__ == "__main__":
    app.run(port=5000)
