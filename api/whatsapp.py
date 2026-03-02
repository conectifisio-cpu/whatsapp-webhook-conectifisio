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
# CONFIGURAÇÕES DE AMBIENTE (VARIÁVEIS VERCEL)
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN")

# ==========================================
# INICIALIZAÇÃO DO FIREBASE (MEMÓRIA CENTRAL)
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
# FUNÇÕES DE APOIO E MENSAGERIA INTERATIVA
# ==========================================

def update_paciente(phone, data):
    """Atualiza o Firebase garantindo que os dados sejam mesclados (merge)"""
    if not db: return "Erro: Banco offline"
    try:
        data["lastInteraction"] = firestore.SERVER_TIMESTAMP
        db.collection("PatientsKanban").document(phone).set(data, merge=True)
        return "OK"
    except Exception as e: return str(e)

def enviar_whatsapp(to, payload):
    """Envia o payload para a API da Meta"""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    try:
        res = requests.post(url, json={"messaging_product": "whatsapp", "to": to, **payload}, headers=headers, timeout=10)
        return res.json()
    except: return None

def enviar_texto(to, texto):
    """Simula digitação e envia texto"""
    time.sleep(2.0)
    enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

def enviar_botoes(to, texto, botoes):
    """Botões interativos (Max 3)"""
    time.sleep(1.2)
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": [{"type": "reply", "reply": {"id": f"btn_{i}", "title": b[:20]}} for i, b in enumerate(botoes)]}
        }
    }
    enviar_whatsapp(to, payload)

def enviar_lista(to, texto, titulo_botao, secoes):
    """Menu suspenso para escolha de serviços"""
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
# MOTOR DE INTEGRAÇÃO FEEGOW
# ==========================================
def integrar_feegow(paciente_data):
    if not FEEGOW_TOKEN: return False
    base_url = "https://api.feegow.com.br/v1"
    headers = {"Authorization": FEEGOW_TOKEN, "Content-Type": "application/json"}
    
    cpf_puro = re.sub(r'\D', '', str(paciente_data.get("cpf", "")))
    if len(cpf_puro) != 11: return False

    try:
        # Busca/Cria Paciente
        res_search = requests.get(f"{base_url}/pacientes?cpf={cpf_puro}", headers=headers, timeout=5)
        feegow_id = None
        if res_search.status_code == 200 and res_search.json().get('data'):
            feegow_id = res_search.json()['data'][0]['id']
        else:
            payload_p = {
                "nome": paciente_data.get("title", "Paciente WhatsApp"),
                "cpf": cpf_puro,
                "celular": paciente_data.get("cellphone", ""),
                "email": paciente_data.get("email", "")
            }
            res_c = requests.post(f"{base_url}/pacientes", headers=headers, json=payload_p, timeout=5)
            feegow_id = res_c.json().get('data', {}).get('id') if res_c.status_code == 200 else None

        # Autorização de Guia (Avaliação Inicial)
        if feegow_id and paciente_data.get("modalidade") == "Convênio":
            mapa_conv = {"Amil": 3, "Bradesco": 2, "Porto Seguro": 4, "Prevent Senior": 7, "Cassi": 8, "Saúde Caixa": 10154}
            conv_id = mapa_conv.get(paciente_data.get("convenio", ""), 0)
            unidade_id = 1 if paciente_data.get("unit") == "Ipiranga" else 0 # 0=SCS, 1=Ipiranga
            
            payload_auth = {
                "paciente_id": feegow_id,
                "unidade_id": unidade_id,
                "convenio_id": conv_id,
                "procedimento_id": 9, # Avaliação Inicial
                "observacoes": f"🤖 Robô Conectifisio: {paciente_data.get('queixa', '')}"
            }
            requests.post(f"{base_url}/autorizacoes", headers=headers, json=payload_auth, timeout=5)
        return True
    except: return False

# ==========================================
# CÉREBRO PRINCIPAL (WEBHOOK)
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
        
        # Identificação de Unidade via Metadata
        display_phone = value.get("metadata", {}).get("display_phone_number", "")
        unidade_padrao = "Ipiranga" if "23629360" in str(display_phone) else "SCS"

        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            inter = message.get("interactive", {})
            msg_recebida = inter.get("button_reply", {}).get("title", "") or inter.get("list_reply", {}).get("title", "")
        
        msg_lower = msg_recebida.lower().strip()

        # Resgate de Contexto (Firebase)
        doc_ref = db.collection("PatientsKanban").document(phone)
        doc = doc_ref.get()
        info = doc.to_dict() if doc.exists else {"status": "inicio"}
        status = info.get("status", "inicio")

        # Comandos Globais
        if msg_lower in ["reset", "recomeçar", "menu"]: status = "inicio"
        if msg_type == "audio":
            enviar_texto(phone, "Ainda não consigo processar áudios 🎧. Para que eu possa te ajudar agora, por favor utilize os botões ou escreva em texto.")
            return jsonify({"status": "ok"}), 200

        # ==========================================
        # FLUXO DETALHISTA (ESTADOS)
        # ==========================================

        if status == "inicio":
            msg = f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unidade_padrao}. Esta é a unidade que deseja atendimento ou prefere trocar de local?"
            enviar_botoes(phone, msg, ["✅ Continuar aqui", "📍 Trocar Unidade"])
            update_paciente(phone, {"status": "confirmar_unidade", "unit": unidade_padrao, "cellphone": phone})

        elif status == "confirmar_unidade":
            if "trocar" in msg_lower:
                nova = "Ipiranga" if info.get("unit") == "SCS" else "SCS"
                update_paciente(phone, {"unit": nova})
                enviar_texto(phone, f"Perfeito! Unidade alterada para {nova}. ✅\n\nPara começarmos o seu atendimento e garantirmos o seu histórico, por favor, digite o seu **NOME COMPLETO**:")
            else:
                enviar_texto(phone, "Ótimo! ✅ Para começarmos o seu atendimento e garantirmos o seu cadastro, por favor, digite o seu **NOME COMPLETO**:")
            update_paciente(phone, {"status": "aguardando_nome"})

        elif status == "aguardando_nome":
            nome_completo = msg_recebida.title()
            update_paciente(phone, {"title": nome_completo})
            
            secoes = [
                {"title": "Tratamento Clínico", "rows": [{"id": "1", "title": "Fisio Ortopédica"}, {"id": "2", "title": "Fisio Neurológica"}, {"id": "3", "title": "Fisio Pélvica"}]},
                {"title": "Bem-Estar e Estúdio", "rows": [{"id": "4", "title": "Pilates Studio"}, {"id": "5", "title": "Acupuntura"}]}
            ]
            enviar_lista(phone, f"Prazer em conhecer, {nome_completo.split()[0]}! 😊\n\nPara direcionarmos o seu atendimento ao especialista ideal, qual serviço você procura hoje?", "Ver Serviços", secoes)
            update_paciente(phone, {"status": "processar_servico"})

        elif status == "processar_servico":
            servico = msg_recebida
            update_paciente(phone, {"servico": servico})

            # TRIAGEM NEURO (Regra: Riqueza de Detalhes)
            if "neurológica" in servico.lower():
                msg_neuro = (
                    "Queremos garantir que sua experiência na Conectifisio seja a mais confortável e segura possível. 😊\n\n"
                    "Poderia nos contar em qual dessas opções de suporte você se enquadra hoje?\n\n"
                    "1️⃣ *AUXÍLIO INTEGRAL*: Preciso de ajuda de outra pessoa para a maioria das tarefas (como sentar, levantar ou trocar de roupa). Não consigo me movimentar sozinho.\n\n"
                    "2️⃣ *AUXÍLIO PARCIAL*: Consigo fazer algumas coisas, mas utilizo bengala, andador ou preciso de ajuda para algumas atividades específicas.\n\n"
                    "3️⃣ *AUTONOMIA TOTAL*: Consigo realizar minhas tarefas e me movimentar sozinho(a) com total segurança.\n\n"
                    "Sua resposta nos ajuda a preparar a sala e o especialista para você! ✅"
                )
                enviar_botoes(phone, msg_neuro, ["1️⃣ Auxílio Integral", "2️⃣ Auxílio Parcial", "3️⃣ Autonomia Total"])
                update_paciente(phone, {"status": "triagem_neuro"})
            else:
                enviar_texto(phone, "Entendido! Me conte brevemente: o que te trouxe à clínica hoje? Sua resposta nos ajuda a entender sua dor.\n\nExemplo: *Sinto dor na lombar há 2 semanas* ou *Fiz cirurgia no joelho e preciso de reabilitação*.")
                update_paciente(phone, {"status": "aguardando_queixa"})

        elif status == "triagem_neuro":
            if "1️⃣" in msg_recebida or "integral" in msg_lower:
                enviar_texto(phone, "Compreendo perfeitamente. Pela necessidade de suporte integral e para garantir sua segurança, nosso fisioterapeuta coordenador assumirá seu atendimento agora. Aguarde um instante! 👨‍⚕️")
                update_paciente(phone, {"status": "atendimento_humano", "queixa_ia": "[ALERTA: PACIENTE DEPENDENTE NEURO]"})
            else:
                enviar_texto(phone, "Recebido! Me conte brevemente: qual o diagnóstico ou principal queixa hoje?\n\nExemplo: *Reabilitação após AVC* ou *Dificuldade de equilíbrio por Parkinson*.")
                update_paciente(phone, {"status": "aguardando_queixa"})

        elif status == "aguardando_queixa":
            update_paciente(phone, {"queixa": msg_recebida})
            enviar_botoes(phone, "Entendido. Faremos o melhor para cuidar de você! 💙\nDeseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", ["💳 Convênio", "💎 Particular"])
            update_paciente(phone, {"status": "escolha_modalidade"})

        elif status == "escolha_modalidade":
            modalidade = "Convênio" if "conv" in msg_lower else "Particular"
            update_paciente(phone, {"modalidade": modalidade})
            enviar_texto(phone, "Ótimo! Para iniciarmos seu cadastro oficial, digite seu **CPF** (apenas os 11 números, sem pontos ou traços).\n\nExemplo: *12345678901*")
            update_paciente(phone, {"status": "aguardando_cpf"})

        elif status == "aguardando_cpf":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) != 11:
                enviar_texto(phone, "⚠️ *CPF parece estar incompleto.*\n\nO CPF precisa ter exatamente 11 números. (Não se preocupe com pontos ou traços, eu limpo para você! 😊)\n\nExemplo: *12345678901*. Tente novamente:")
            else:
                update_paciente(phone, {"cpf": cpf_limpo})
                enviar_texto(phone, "CPF validado! ✅ Agora, qual sua **Data de Nascimento**? (Use as barras, por favor).\n\nExemplo: *15/05/1980*")
                update_paciente(phone, {"status": "aguardando_nascimento"})

        elif status == "aguardando_nascimento":
            if not re.match(r"^\d{2}/\d{2}/\d{4}$", msg_recebida):
                enviar_texto(phone, "⚠️ *Formato incorreto de data.*\n\nPor favor, digite dia, mês e ano com as barras para o sistema aceitar.\n\nExemplo: *15/05/1980*. Pode digitar de novo?")
            else:
                update_paciente(phone, {"birthDate": msg_rece_bida})
                enviar_texto(phone, "Obrigado! Para finalizarmos, qual o seu melhor **E-MAIL**?\n\nExemplo: *nome@email.com*")
                update_paciente(phone, {"status": "aguardando_email"})

        elif status == "aguardando_email":
            if "@" not in msg_recebida or "." not in msg_rece_bida:
                enviar_texto(phone, "⚠️ *O e-mail parece estar incompleto.*\n\nVerifique se digitou o '@' e o domínio corretamente.\n\nExemplo: *seu-nome@gmail.com*. Digite novamente:")
            else:
                update_paciente(phone, {"email": msg_recebida, "status": "finalizado"})
                enviar_texto(phone, "Tudo pronto! 🎉 Seu pré-cadastro foi concluído.\n\nA nossa equipe de recepção vai assumir o atendimento agora para confirmar os horários e finalizar sua marcação. Aguarde um instante! 👩‍⚕️")
                integrar_feegow({**info, "email": msg_recebida, "cpf": info.get("cpf")})

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"❌ ERRO CRÍTICO: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(port=5000)
