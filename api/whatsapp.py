import os
import json
import traceback
import re
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore
import requests

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE (VERCEL)
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN")

# ==========================================
# INICIALIZAÇÃO FIREBASE (FONTE DA VERDADE)
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
        print(f"❌ ERRO FIREBASE: {e}")

db = firestore.client() if firebase_admin._apps else None

# ==========================================
# DICIONÁRIOS E MAPAS (BÍBLIA DA CLÍNICA)
# ==========================================
MAPA_CONV_FEEGOW = {
    "Bradesco Saúde": 2, "Amil": 3, "Porto Seguro Saúde": 4,
    "GEAP": 6, "Prevent Senior": 7, "Cassi": 8,
    "Saúde Petrobras": 11, "Mediservice": 9968, "Saúde Caixa": 10154
}

MAPA_COBERTURA = {
    "Amil": ["Fisio Ortopédica", "Fisio Neurológica"],
    "Bradesco Saúde": ["Fisio Ortopédica", "Fisio Neurológica"],
    "Porto Seguro Saúde": ["Fisio Ortopédica", "Fisio Neurológica"],
    "Prevent Senior": ["Fisio Ortopédica", "Fisio Neurológica", "Fisio Pélvica", "Acupuntura"],
    "Saúde Caixa": ["Fisio Ortopédica", "Fisio Neurológica", "Fisio Pélvica", "Acupuntura", "Pilates Studio"],
    "Saúde Petrobras": ["Fisio Ortopédica", "Fisio Neurológica", "Fisio Pélvica"],
    "Cassi": ["Fisio Ortopédica", "Fisio Neurológica", "Fisio Pélvica"],
    "Mediservice": ["Fisio Ortopédica", "Fisio Neurológica"],
    "GEAP": ["Fisio Ortopédica", "Fisio Neurológica", "Acupuntura"],
    "Wellhub": ["Pilates Studio"],
    "TotalPass": ["Pilates Studio"]
}

# ==========================================
# FUNÇÕES DE APOIO E MENSAGERIA
# ==========================================
def update_paciente(phone, data):
    if not db: return "Erro: Banco offline"
    try:
        data["lastInteraction"] = firestore.SERVER_TIMESTAMP
        db.collection("PatientsKanban").document(phone).set(data, merge=True)
        return "OK"
    except Exception as e: return str(e)

def enviar_whatsapp(to, payload):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    try: requests.post(url, json={"messaging_product": "whatsapp", "to": to, **payload}, headers=headers, timeout=10)
    except: pass

def enviar_texto(to, texto):
    time.sleep(1.5)
    enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

def enviar_botoes(to, texto, botoes):
    time.sleep(1.0)
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": [{"type": "reply", "reply": {"id": f"b_{i}", "title": b[:20]}} for i, b in enumerate(botoes)]}
        }
    }
    enviar_whatsapp(to, payload)

def enviar_lista(to, texto, titulo_botao, secoes):
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": texto},
            "action": {"button": titulo_botao, "sections": secoes}
        }
    }
    enviar_whatsapp(to, payload)

# ==========================================
# MENUS MODULARES (PADRONIZAÇÃO 157)
# ==========================================
def get_secoes_especialidades():
    return [
        {"title": "Tratamento Clínico", "rows": [{"id": "s1", "title": "Fisio Ortopédica"}, {"id": "s2", "title": "Fisio Neurológica"}, {"id": "s3", "title": "Fisio Pélvica"}, {"id": "s4", "title": "Acupuntura"}]},
        {"title": "Bem-Estar e Estúdio", "rows": [{"id": "s5", "title": "Pilates Studio"}, {"id": "s6", "title": "Recovery"}, {"id": "s7", "title": "Liberação Miofascial"}]}
    ]

def get_secoes_veterano():
    return [{"title": "Opções de Atendimento", "rows": [{"id": "v1", "title": "🗓️ Reagendar Sessão"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v4", "title": "📁 Outras Solicitações"}]}]

def get_secoes_convenios():
    return [{"title": "Planos Atendidos", "rows": [{"id": f"c{i}", "title": c} for i, c in enumerate(MAPA_CONV_FEEGOW.keys())] + [{"id": "c99", "title": "Outro / Não listado"}]}]

# ==========================================
# MOTOR DE INTEGRAÇÃO FEEGOW
# ==========================================
def integrar_feegow(paciente_data):
    if not FEEGOW_TOKEN: return False
    base_url = "https://api.feegow.com.br/v1"
    headers = {"Authorization": FEEGOW_TOKEN, "Content-Type": "application/json"}
    cpf_puro = re.sub(r'\D', '', str(paciente_data.get("cpf", "")))
    if len(cpf_puro) != 11: return False

    try:
        # 1. Busca/Cria Paciente
        res_s = requests.get(f"{base_url}/pacientes?cpf={cpf_puro}", headers=headers, timeout=5)
        feegow_id = res_s.json()['data'][0]['id'] if res_s.status_code == 200 and res_s.json().get('data') else None
        
        if not feegow_id:
            p_payload = {"nome": paciente_data.get("title"), "cpf": cpf_puro, "celular": paciente_data.get("cellphone"), "email": paciente_data.get("email")}
            res_c = requests.post(f"{base_url}/pacientes", headers=headers, json=p_payload, timeout=5)
            feegow_id = res_c.json().get('data', {}).get('id')

        # 2. Autorização (Se Convénio)
        if feegow_id and paciente_data.get("modalidade") == "Convénio":
            conv_id = MAPA_CONV_FEEGOW.get(paciente_data.get("convenio"), 0)
            unidade_id = 1 if paciente_data.get("unit") == "Ipiranga" else 0
            proc_id = 21 if "acupuntura" in str(paciente_data.get("servico")).lower() else 9
            
            auth_payload = {
                "paciente_id": feegow_id, "unidade_id": unidade_id, "convenio_id": conv_id, 
                "procedimento_id": proc_id, "carteirinha": paciente_data.get("numCarteirinha", ""),
                "observacoes": f"🤖 Robô WhatsApp: {paciente_data.get('queixa', '')}"
            }
            requests.post(f"{base_url}/autorizacoes", headers=headers, json=auth_payload, timeout=5)
        return True
    except: return False

# ==========================================
# CÉREBRO MASTER (A FUSÃO)
# ==========================================
@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return jsonify({"status": "ignore"}), 200

        message = value["messages"][0]
        phone = message["from"]
        msg_type = message.get("type", "text")
        
        display_phone = value.get("metadata", {}).get("display_phone_number", "")
        unidade_auto = "Ipiranga" if "23629360" in str(display_phone) else "SCS"

        msg_recebida = ""
        if msg_type == "text": msg_recebida = message.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            inter = message.get("interactive", {})
            msg_recebida = inter.get("button_reply", {}).get("title", "") or inter.get("list_reply", {}).get("title", "")
        
        msg_lower = msg_recebida.lower().strip()

        # Resgate de Memória
        doc_ref = db.collection("PatientsKanban").document(phone)
        doc = doc_ref.get()
        info = doc.to_dict() if doc.exists else {"status": "inicio"}
        status = info.get("status", "inicio")
        is_veterano = len(re.sub(r'\D', '', info.get("cpf", ""))) == 11

        # 🛡️ ESCUDOS (ÁUDIO E ANTI-LIXO)
        if msg_type == "audio":
            enviar_texto(phone, "Ainda não consigo ouvir áudios 🎧. Por favor, utilize os botões ou digite sua resposta para que eu possa te ajudar agora.")
            return jsonify({"status": "ok"}), 200

        if msg_lower in ["reset", "recomeçar", "menu"]:
            status = "inicio"
        elif status not in ["inicio", "menu_veterano", "finalizado", "atendimento_humano"] and msg_lower in ["oi", "olá", "bom dia"]:
            enviar_botoes(phone, "Notei que estávamos no meio do seu atendimento. Como deseja prosseguir?", ["✅ Sim, continuar", "🔄 Recomeçar"])
            return jsonify({"status": "ok"}), 200

        # --- FLUXO DE ESTADOS ---

        if status == "inicio":
            enviar_botoes(phone, f"Olá! ✨ Seja bem-vindo à Conectifisio unidade {unidade_auto}. Deseja atendimento nesta unidade?", ["✅ Continuar aqui", "📍 Trocar Unidade"])
            update_paciente(phone, {"status": "confirmar_unidade", "unit": unidade_auto, "cellphone": phone})

        elif status == "confirmar_unidade":
            if "trocar" in msg_lower:
                nova = "Ipiranga" if info.get("unit") == "SCS" else "SCS"
                update_paciente(phone, {"unit": nova})
                enviar_texto(phone, f"Unidade alterada para {nova}! ✅\n\nPor favor, digite o seu **NOME COMPLETO**:")
            else:
                enviar_texto(phone, "Perfeito! ✅ Por favor, digite o seu **NOME COMPLETO**:")
            update_paciente(phone, {"status": "aguardando_nome"})

        elif status == "aguardando_nome":
            nome = msg_recebida.title()
            update_paciente(phone, {"title": nome})
            if is_veterano:
                enviar_lista(phone, f"Olá, {nome.split()[0]}! ✨ Que bom ter você de volta. Em que posso te ajudar hoje?", "Abrir Menu", get_secoes_veterano())
                update_paciente(phone, {"status": "menu_veterano"})
            else:
                enviar_lista(phone, f"Prazer, {nome.split()[0]}! 😊 Qual serviço você procura hoje?", "Ver Serviços", get_secoes_especialidades())
                update_paciente(phone, {"status": "processar_servico"})

        elif status == "menu_veterano":
            if "reagendar" in msg_lower:
                enviar_botoes(phone, "Qual o melhor período para a nova sessão?", ["☀️ Manhã", "⛅ Tarde"])
                update_paciente(phone, {"status": "atendimento_humano", "queixa": "[PEDIDO REAGENDAMENTO]"})
            elif "guia" in msg_lower:
                enviar_botoes(phone, "A nova guia será pelo CONVÉNIO ou PARTICULAR?", ["💳 Convénio", "💎 Particular"])
                update_paciente(phone, {"status": "escolha_modalidade"})
            elif "novo" in msg_lower:
                enviar_lista(phone, "Qual novo serviço deseja conhecer?", "Ver Serviços", get_secoes_especialidades())
                update_paciente(phone, {"status": "processar_servico"})
            else:
                enviar_texto(phone, "Entendido! Um de nossos rececionistas já vai te atender. 👩‍⚕️")
                update_paciente(phone, {"status": "atendimento_humano"})

        elif status == "processar_servico":
            servico = msg_recebida
            update_paciente(phone, {"servico": servico})
            
            # Atalho Premium
            if servico in ["Recovery", "Liberação Miofascial"]:
                update_paciente(phone, {"modalidade": "Particular", "status": "aguardando_queixa"})
                enviar_texto(phone, f"Ótima escolha! O serviço de {servico} é focado em performance e realizado de forma PARTICULAR. 💎\n\nMe conte: o que te trouxe à clínica hoje?")
            
            # Trava Pilates
            elif servico == "Pilates Studio":
                if info.get("unit") == "Ipiranga":
                    enviar_botoes(phone, "O Pilates é exclusivo da unidade SCS. 🧘‍♀️ Deseja transferir o atendimento?", ["✅ Mudar p/ SCS", "❌ Outro Serviço"])
                    update_paciente(phone, {"status": "pilates_transfer"})
                else:
                    enviar_botoes(phone, "Excelente! 🧘‍♀️ Como pretende realizar as aulas?", ["💎 Particular", "🏦 Saúde Caixa", "💪 Wellhub/Totalpass"])
                    update_paciente(phone, {"status": "pilates_modalidade"})
            
            # Triagem Neuro
            elif "neurológica" in servico.lower():
                msg_neuro = (
                    "Queremos garantir que sua experiência seja a mais segura possível. 😊\n\n"
                    "Em qual destas opções você se enquadra hoje?\n\n"
                    "1️⃣ *AUXÍLIO INTEGRAL*: Preciso de ajuda de outra pessoa para quase tudo (sentar, levantar, trocar de roupa).\n"
                    "2️⃣ *AUXÍLIO PARCIAL*: Uso bengala, andador ou preciso de ajuda para algumas atividades.\n"
                    "3️⃣ *AUTONOMIA TOTAL*: Me movimento sozinho(a) e realizo tarefas com segurança.\n\n"
                    "Sua resposta nos ajuda a preparar tudo! ✅"
                )
                enviar_botoes(phone, msg_neuro, ["1️⃣ Auxílio Integral", "2️⃣ Auxílio Parcial", "3️⃣ Autonomia Total"])
                update_paciente(phone, {"status": "triagem_neuro"})
            else:
                enviar_texto(phone, "Entendido! O que te trouxe à clínica hoje? (Ex: dor lombar, pós-cirúrgico...)")
                update_paciente(phone, {"status": "aguardando_queixa"})

        elif status == "triagem_neuro":
            if "1️⃣" in msg_recebida:
                enviar_texto(phone, "Pela necessidade de auxílio integral, nosso fisioterapeuta assumirá o atendimento agora. Aguarde! 👨‍⚕️")
                update_paciente(phone, {"status": "atendimento_humano", "queixa": "[DEPENDENTE NEURO]"})
            else:
                enviar_texto(phone, "Entendido! Qual o diagnóstico ou queixa principal? (Ex: AVC, Parkinson...)")
                update_paciente(phone, {"status": "aguardando_queixa"})

        elif status == "aguardando_queixa":
            update_paciente(phone, {"queixa": msg_recebida})
            if info.get("modalidade") == "Particular":
                enviar_texto(phone, "Obrigado! Para o cadastro, digite o seu **CPF** (apenas números).\n\nExemplo: *12345678901*")
                update_paciente(phone, {"status": "aguardando_cpf"})
            else:
                enviar_botoes(phone, "Deseja realizar o atendimento pelo seu CONVÉNIO ou de forma PARTICULAR?", ["💳 Convénio", "💎 Particular"])
                update_paciente(phone, {"status": "escolha_modalidade"})

        elif status == "escolha_modalidade":
            if "particular" in msg_lower:
                update_paciente(phone, {"modalidade": "Particular", "status": "aguardando_cpf"})
                enviar_texto(phone, "Ótimo! Digite seu **CPF** (11 números).\n\nExemplo: *12345678901*")
            else:
                update_paciente(phone, {"modalidade": "Convénio", "status": "aguardando_convenio"})
                enviar_lista(phone, "Selecione o seu plano de saúde na lista abaixo:", "Ver Planos", get_secoes_convenios())

        elif status == "aguardando_convenio":
            plano = msg_recebida
            servico = info.get("servico", "")
            if servico not in MAPA_COBERTURA.get(plano, []):
                msg_e = f"O plano *{plano}* não cobre *{servico}* diretamente. 😔\n\nMas fazemos no Particular com recibo para reembolso. Deseja seguir?"
                enviar_botoes(phone, msg_e, ["✅ Sim, Particular", "❌ Não, obrigado"])
                update_paciente(phone, {"status": "valida_reembolso", "convenio": plano})
            else:
                update_paciente(phone, {"convenio": plano, "status": "aguardando_cpf"})
                enviar_texto(phone, f"Plano {plano} validado! ✅ Digite seu **CPF** (11 números).\n\nExemplo: *12345678901*")

        elif status == "aguardando_cpf":
            cpf = re.sub(r'\D', '', msg_recebida)
            if len(cpf) != 11:
                enviar_texto(phone, "⚠️ *CPF Inválido.* Digite os 11 números. Exemplo: *12345678901*")
            else:
                update_paciente(phone, {"cpf": cpf, "status": "aguardando_nascimento"})
                enviar_texto(phone, "CPF validado! ✅ Qual sua **Data de Nascimento**?\n\nExemplo: *15/05/1980*")

        elif status == "aguardando_nascimento":
            if not re.match(r"^\d{2}/\d{2}/\d{4}$", msg_recebida):
                enviar_texto(phone, "⚠️ *Formato incorreto.* Use dia/mês/ano. Exemplo: *15/05/1980*")
            else:
                update_paciente(phone, {"birthDate": msg_recebida, "status": "aguardando_email"})
                enviar_texto(phone, "Obrigado! Qual o seu melhor **E-MAIL**?\n\nExemplo: *nome@email.com*")

        elif status == "aguardando_email":
            if "@" not in msg_recebida or "." not in msg_recebida:
                enviar_texto(phone, "⚠️ *E-mail incompleto.* Exemplo: *paciente@gmail.com*")
            else:
                update_paciente(phone, {"email": msg_recebida})
                if info.get("modalidade") == "Convénio":
                    enviar_texto(phone, "Cadastro concluído! 🎉 Agora, digite o **NÚMERO DA CARTEIRINHA** do seu plano:")
                    update_paciente(phone, {"status": "aguardando_num_carteirinha"})
                else:
                    enviar_texto(phone, "Tudo pronto! 🎉 Sua ficha foi criada. Nossa receção já vai te atender para confirmar o horário. Aguarde!")
                    update_paciente(phone, {"status": "finalizado"})
                    integrar_feegow({**info, "email": msg_recebida})

        elif status == "aguardando_num_carteirinha":
            update_paciente(phone, {"numCarteirinha": msg_recebida, "status": "aguardando_docs"})
            enviar_texto(phone, "Número registado! ✅ Agora, envie uma **FOTO ou PDF** da sua Carteirinha e do seu Pedido Médico. 📸")

        elif status == "aguardando_docs":
            if msg_type in ['image', 'document']:
                enviar_texto(phone, "Documento recebido! ✅ Já estamos a processar. Um de nossos rececionistas confirmará seu horário em instantes.")
                update_paciente(phone, {"status": "finalizado"})
                integrar_feegow(info)
            else:
                enviar_texto(phone, "Por favor, utilize o anexo para enviar a FOTO ou o PDF dos documentos. 📸")

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"❌ ERRO: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(port=5000)
