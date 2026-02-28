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
    except Exception as e:
        print(f"❌ ERRO FIREBASE INIT: {e}")

db = firestore.client() if firebase_admin._apps else None

# ==========================================
# FUNÇÃO DE SALVAMENTO FIREBASE
# ==========================================
def update_paciente(phone, data):
    if not db: return
    try:
        data["lastInteraction"] = firestore.SERVER_TIMESTAMP
        db.collection("PatientsKanban").document(phone).set(data, merge=True)
    except Exception as e:
        pass

# ==========================================
# 🚀 MOTOR DE CONSULTA FEEGOW (15 DIAS)
# ==========================================
def consultar_agenda_feegow(cpf):
    if not FEEGOW_TOKEN or not cpf: return [], "CPF ou Token ausente.", "Sem dados"
    cpf_limpo = re.sub(r'\D', '', str(cpf))
    
    headers_br = {"Authorization": FEEGOW_TOKEN, "Content-Type": "application/json"}
    headers_old = {"x-access-token": FEEGOW_TOKEN, "Content-Type": "application/json"}
    
    paciente_id = None
    log_debug = []
    
    # PASSO 1: DESCOBRIR ID DO PACIENTE
    try: 
        r1 = requests.get(f"https://api.feegow.com.br/v1/pacientes?cpf={cpf_limpo}", headers=headers_br, timeout=5)
        if r1.status_code == 200 and r1.json().get("data"):
            paciente_id = r1.json()["data"][0]["id"]
            log_debug.append(f"ID achado na Rota Nova: {paciente_id}")
    except: pass
    
    if not paciente_id:
        try: 
            r3 = requests.get(f"https://api.feegow.com/v1/api/patient/search?paciente_cpf={cpf_limpo}&photo=false", headers=headers_old, timeout=5)
            if r3.status_code == 200 and r3.json().get("content"):
                c = r3.json().get("content", {})
                if isinstance(c, list) and len(c) > 0: paciente_id = c[0].get("id") or c[0].get("paciente_id")
                elif isinstance(c, dict): paciente_id = c.get("id") or c.get("paciente_id")
                log_debug.append(f"ID achado na Rota Antiga: {paciente_id}")
        except: pass

    if not paciente_id:
        return [], "Paciente não localizado.", "\n".join(log_debug)

    # PASSO 2: BUSCAR AGENDAMENTOS FUTUROS (Janela de 15 Dias Exata)
    hoje = datetime.now()
    d_start = hoje.strftime('%Y-%m-%d')
    d_end = (hoje + timedelta(days=15)).strftime('%Y-%m-%d')
    
    log_debug.append(f"Buscando de {d_start} até {d_end}")

    rotas_agenda = [
        (f"https://api.feegow.com.br/v1/appoints/search?paciente_id={paciente_id}&data_start={d_start}&data_end={d_end}", headers_br, "BR_Search"),
        (f"https://api.feegow.com.br/v1/appoints?paciente_id={paciente_id}&data_start={d_start}&data_end={d_end}", headers_br, "BR_Appoints"),
        (f"https://api.feegow.com/v1/api/appoints/search?paciente_id={paciente_id}&data_start={d_start}&data_end={d_end}", headers_old, "OLD_Search"),
        (f"https://api.feegow.com/v1/api/appoints?paciente_id={paciente_id}&data={d_start}", headers_old, "OLD_Appoints_Single")
    ]

    for url, hdrs, nome_rota in rotas_agenda:
        try:
            res = requests.get(url, headers=hdrs, timeout=5)
            log_debug.append(f"Rota {nome_rota}: HTTP {res.status_code}")
            
            if res.status_code == 200:
                dados = res.json()
                itens = dados.get("data") or dados.get("content") or []
                if isinstance(itens, dict): itens = [itens] 
                
                log_debug.append(f"-> Retornou {len(itens)} itens")
                
                sessoes = []
                for a in itens:
                    status = str(a.get("status_nome", a.get("status", ""))).lower()
                    if "cancelado" not in status and "falta" not in status:
                        data_raw = str(a.get("data", "")).split("T")[0]
                        if data_raw >= d_start:
                            proc = a.get("procedimento_nome") or a.get("procedimento", {}).get("nome", "Sessão")
                            hora = str(a.get("horario", a.get("hora", "")))[:5]
                            dt_obj = datetime.strptime(data_raw, "%Y-%m-%d")
                            sessoes.append(f"🗓️ *{dt_obj.strftime('%d/%m/%Y')} às {hora}* - {proc}")
                
                if sessoes: 
                    log_debug.append(f"-> SUCESSO: Achou {len(sessoes)} sessões ativas!")
                    return sessoes[:3], str(paciente_id), "\n".join(log_debug)
        except Exception as e: 
            log_debug.append(f"Rota {nome_rota} falhou localmente.")
            continue
        
    return [], str(paciente_id), "\n".join(log_debug)

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
            
            enviar_texto(phone, "Estou consultando a sua agenda na clínica (Raio-X ativado)... um instante. ⏳")
            sessoes, info_id, log_debug = consultar_agenda_feegow(cpf_paciente)
            
            if sessoes:
                msg = f"✅ SUCESSO! \n\nLocalizei suas próximas sessões: 👇\n\n" + "\n".join(sessoes) + "\n\nQual delas gostaria de reagendar?"
                enviar_botoes(phone, msg, ["A Primeira", "Outra Data", "Falar com Recepção"])
            else:
                # O Raio-X entra em acção se falhar
                msg_falha = f"🔍 *RAIO-X DO FEEGOW*\nID Paciente: {info_id}\n\n*Log da API:*\n{log_debug}\n\n⚠️ Não encontrei sessões nos próximos 15 dias. Deseja agendar agora?"
                enviar_botoes(phone, msg_falha, ["☀️ Manhã", "⛅ Tarde", "⬅️ Voltar"])
            return jsonify({"status": "ok"}), 200

        # 5. MENU INICIAL
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
