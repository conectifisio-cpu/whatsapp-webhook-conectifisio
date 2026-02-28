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
# CONFIGURAÇÕES DE AMBIENTE (VERCEL)
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# ==========================================
# INICIALIZAÇÃO DO FIREBASE (SESSÃO E ESTADO)
# ==========================================
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
if firebase_creds_json and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_creds_json, strict=False)
        if 'private_key' in cred_dict:
            cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    except: pass

db = firestore.client() if firebase_admin._apps else None

# ==========================================
# MOTOR DE INTELIGÊNCIA ARTIFICIAL (GEMINI)
# ==========================================
def chamar_gemini_empatia(queixa):
    """Gera uma resposta acolhedora baseada na dor do paciente"""
    if not GEMINI_API_KEY: return "Entendido. Vamos cuidar disso para você agora mesmo."
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": f"Você é a recepção da clínica Conectifisio. O paciente relatou a seguinte queixa: '{queixa}'. Responda com uma única frase curta e muito empática, demonstrando que a clínica é especialista nisso. Não faça perguntas."}]}]
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        return res.json()['candidates'][0]['content']['parts'][0]['text'].strip()
    except:
        return "Sinto muito por esse desconforto. Vamos agendar sua avaliação para resolver isso logo."

# ==========================================
# MOTOR DE CONSULTA FEEGOW (DIRETO)
# ==========================================
def consultar_feegow_direto(feegow_id):
    """Consulta a agenda oficial do paciente sem intermediários"""
    if not FEEGOW_TOKEN or not feegow_id: return [], "Dados de acesso ausentes."

    # Cabeçalhos de Proteção para evitar o Erro 403 (Bypass Cloudflare)
    headers = {
        "x-access-token": FEEGOW_TOKEN,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://feegow.com.br/"
    }

    hoje = datetime.now()
    d_start = hoje.strftime('%Y-%m-%d')
    d_end = (hoje + timedelta(days=60)).strftime('%Y-%m-%d')

    # Rota Oficial de Busca de Agendamentos
    url = f"https://api.feegow.com/v1/api/appoints/search?paciente_id={feegow_id}&data_start={d_start}&data_end={d_end}"
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            dados = r.json()
            itens = dados.get("content") or dados.get("data") or []
            lista_final = []
            for a in itens:
                if "cancelado" not in str(a.get("status_nome", "")).lower():
                    data_pt = datetime.strptime(str(a.get("data", ""))[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
                    hora = str(a.get("horario", ""))[:5]
                    proc = a.get("procedimento_nome", "Sessão")
                    lista_final.append(f"🗓️ *{data_pt} às {hora}* - {proc}")
            return lista_final[:5], ""
        return [], f"Erro {r.status_code} na API."
    except Exception as e:
        return [], str(e)

# ==========================================
# GESTÃO DE WHATSAPP (ENTRADA/SAÍDA)
# ==========================================
def enviar_whatsapp(to, payload_msg):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    try: requests.post(url, json={"messaging_product": "whatsapp", "to": to, **payload_msg}, timeout=10)
    except: pass

def responder_texto(to, texto):
    enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

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
        msg_recebida = message.get("text", {}).get("body", "").strip() or \
                       message.get("interactive", {}).get("button_reply", {}).get("title", "")

        # --- SISTEMA DE RESET ---
        if msg_recebida.lower() in ["reset", "recomeçar", "menu"]:
            if db: db.collection("PatientsKanban").document(phone).delete()
            enviar_botoes(phone, "Atendimento reiniciado! Como posso ajudar?", [{"id":"v1","title":"🗓️ Agendamentos"},{"id":"v2","title":"🔄 Nova Guia"}])
            return jsonify({"status": "reset"}), 200

        # --- RECUPERAÇÃO DE ESTADO ---
        doc_ref = db.collection("PatientsKanban").document(phone)
        doc = doc_ref.get()
        info = doc.to_dict() if doc.exists else {"status": "inicio", "feegow_id": "2279"}

        # --- LÓGICA DE FLUXO ---
        # 1. CONSULTA DE AGENDA
        if "Agendamentos" in msg_recebida or "Consultar" in msg_recebida:
            fid = info.get("feegow_id", "2279") # 2279 = Marcel (Teste)
            responder_texto(phone, "Consultando sua agenda diretamente no sistema... ⏳")
            
            lista, erro = consultar_feegow_direto(fid)
            if lista:
                resumo = "Localizei seus próximos horários: 👇\n\n" + "\n".join(lista)
                enviar_botoes(phone, resumo, [{"id":"ok","title":"👍 Confirmar"},{"id":"alt","title":"🔄 Reagendar"}])
            else:
                msg_falha = "Não encontrei agendamentos futuros para você. 🤔\n\nDeseja marcar um novo agora?"
                enviar_botoes(phone, msg_falha, [{"id":"m","title":"Manhã"},{"id":"t","title":"Tarde"}])
            return jsonify({"status": "success"}), 200

        # 2. ESCUTA ATIVA (QUEIXA)
        elif info.get("status") == "esperando_queixa":
            info["queixa"] = msg_recebida
            info["status"] = "escolhendo_modalidade"
            doc_ref.set(info, merge=True)
            
            empatia = chamar_gemini_empatia(msg_recebida)
            responder_texto(phone, empatia)
            enviar_botoes(phone, "Como deseja realizar o atendimento?", [{"id":"c1","title":"💳 Convênio"},{"id":"p1","title":"💎 Particular"}])

        # 3. SAUDAÇÃO INICIAL (SE NADA ACIMA BATER)
        else:
            info["status"] = "esperando_queixa"
            doc_ref.set(info, merge=True)
            enviar_botoes(phone, f"Olá! ✨ Bem-vindo à Conectifisio. Para agilizarmos, me conte brevemente o que te trouxe à clínica hoje?", [{"id":"ag","title":"🗓️ Agendamentos"}])

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ ERRO CRÍTICO: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro": return request.args.get("hub.challenge"), 200
    return "Acesso Negado", 403

if __name__ == "__main__":
    app.run(port=5000)
