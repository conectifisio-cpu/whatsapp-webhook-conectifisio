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
# CONFIGURAÇÕES DE AMBIENTE (VERCEL)
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN", "")

# ==========================================
# INICIALIZAÇÃO FIREBASE (ULTRARRÁPIDO)
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
        print(f"Erro Firebase: {e}")

db = firestore.client() if firebase_admin._apps else None

# ==========================================
# 🚀 MOTOR DE CONSULTA FEEGOW (.com.br)
# ==========================================
def consultar_agenda_feegow(cpf):
    """
    Busca a agenda direto na API Nova do Feegow, que NÃO possui 
    o bloqueio agressivo de Firewall (Cloudflare) contra a Vercel.
    """
    if not FEEGOW_TOKEN or not cpf:
        return [], "CPF ou Token não identificados."

    cpf_limpo = re.sub(r'\D', '', str(cpf))
    if len(cpf_limpo) != 11:
        return [], "CPF inválido na base de dados."

    headers_br = {
        "Authorization": FEEGOW_TOKEN,
        "Content-Type": "application/json"
    }
    base_url = "https://api.feegow.com.br/v1"

    # PASSO 1: Descobrir o ID do Paciente (Esta rota funciona 100%)
    try:
        res_pac = requests.get(f"{base_url}/pacientes?cpf={cpf_limpo}", headers=headers_br, timeout=8)
        if res_pac.status_code != 200 or not res_pac.json().get("data"):
            return [], "Paciente não localizado no sistema Feegow."
        paciente_id = res_pac.json()["data"][0]["id"]
    except Exception as e:
        return [], "Falha na comunicação com o servidor da clínica."

    # PASSO 2: Buscar Agendamentos (Tiro triplo em rotas para evitar erros 404/422)
    hoje = datetime.now().strftime("%Y-%m-%d")
    rotas = [
        f"{base_url}/appoints?paciente_id={paciente_id}&data_start={hoje}",
        f"{base_url}/agendamentos?paciente_id={paciente_id}",
        # Fallback para API Antiga caso a nova não responda
        f"https://api.feegow.com/v1/api/appoints?paciente_id={paciente_id}&data={hoje}"
    ]

    for url in rotas:
        try:
            # Se for a rota antiga, precisa do header x-access-token
            req_headers = {"x-access-token": FEEGOW_TOKEN, "User-Agent": "Mozilla/5.0"} if "api.feegow.com/v1/api" in url else headers_br
            
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
                            proc = a.get("procedimento_nome") or a.get("procedimento", {}).get("nome", "Sessão Clínica")
                            hora = str(a.get("horario", a.get("hora", "")))[:5]
                            dt_obj = datetime.strptime(data_raw, "%Y-%m-%d")
                            sessoes.append(f"🗓️ *{dt_obj.strftime('%d/%m/%Y')} às {hora}* - {proc}")
                
                if sessoes:
                    return sessoes[:3], ""  # Devolve as 3 sessões mais próximas
        except Exception:
            continue

    return [], ""  # Retorna vazio se não encontrou nada, mas sem erro agressivo

# ==========================================
# MENSAGERIA WHATSAPP
# ==========================================
def enviar_whatsapp(to, payload):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    try: requests.post(url, json={"messaging_product": "whatsapp", "to": to, **payload}, timeout=10)
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
        message = data["entry"][0]["changes"][0]["value"]["messages"][0]
        phone = message["from"]
        msg_recebida = message.get("text", {}).get("body", "").strip() or \
                       message.get("interactive", {}).get("button_reply", {}).get("title", "").strip()

        # RECUPERAR MEMÓRIA FIREBASE
        doc_ref = db.collection("PatientsKanban").document(phone) if db else None
        info = doc_ref.get().to_dict() if doc_ref and doc_ref.get().exists else {}

        msg_lower = msg_recebida.lower()

        # ---------------------------------------------------------
        # COMANDO MÁGICO: REAGENDAR SESSÃO (Foco do Dr. Issa)
        # ---------------------------------------------------------
        if "reagendar sessão" in msg_lower or "agendamentos" in msg_lower:
            enviar_texto(phone, "Estou consultando a sua agenda diretamente no sistema da clínica... um instante. ⏳")
            
            cpf_paciente = info.get("cpf")
            if not cpf_paciente:
                # Fallback: Se o paciente for de teste e não tiver CPF
                enviar_texto(phone, "Preciso do seu CPF para consultar o sistema. Por favor, digite os 11 números:")
                if doc_ref: doc_ref.set({"status": "aguardando_cpf_agenda"}, merge=True)
                return jsonify({"status": "ok"}), 200
            
            sessoes, erro = consultar_agenda_feegow(cpf_paciente)
            
            if sessoes:
                msg = "Localizei suas próximas sessões: 👇\n\n" + "\n".join(sessoes) + "\n\nQual delas você gostaria de reagendar?"
                enviar_botoes(phone, msg, ["A Primeira", "Outra Data", "Falar com Recepção"])
            else:
                msg_falha = "Não encontrei agendamentos futuros no sistema."
                if erro: msg_falha += f" ({erro})"
                msg_falha += "\n\nMas não se preocupe, vamos resolver e agendar agora! Qual o melhor período para você?"
                enviar_botoes(phone, msg_falha, ["☀️ Manhã", "⛅ Tarde", "⬅️ Voltar"])
            
            return jsonify({"status": "ok"}), 200

        # RESET
        if msg_lower in ["reset", "recomeçar", "menu inicial"]:
            if doc_ref: doc_ref.delete()
            enviar_botoes(phone, "Atendimento reiniciado! Como posso ajudar hoje?", ["🗓️ Reagendar Sessão", "➕ Novo Serviço"])
            return jsonify({"status": "ok"}), 200

        # MENU INICIAL VETERANO SIMULADO
        cpf = info.get("cpf", "")
        if len(re.sub(r'\D', '', str(cpf))) >= 11:
            enviar_botoes(phone, f"Olá! ✨ Que bom ter você de volta na Conectifisio. Como posso ajudar?", ["🗓️ Reagendar Sessão", "🔄 Nova Guia", "➕ Novo Serviço"])
        else:
            # Menu Inicial Novo
            enviar_botoes(phone, "Olá! ✨ Seja bem-vindo à Conectifisio. Qual serviço procura hoje?", ["Fisio Ortopédica", "Pilates Studio", "Recovery"])

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"Erro Crítico: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    return request.args.get("hub.challenge", "Acesso Negado"), 200

if __name__ == "__main__":
    app.run(port=5000)
