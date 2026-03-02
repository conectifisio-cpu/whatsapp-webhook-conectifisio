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
# INICIALIZAÇÃO DO FIREBASE (MEMÓRIA DO BOT)
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
    """Guarda ou atualiza os dados do paciente no Firebase usando Merge para não apagar nada"""
    if not db: return "Erro: Banco offline"
    try:
        data["lastInteraction"] = firestore.SERVER_TIMESTAMP
        db.collection("PatientsKanban").document(phone).set(data, merge=True)
        return "OK"
    except Exception as e: return str(e)

def enviar_whatsapp(to, payload):
    """Envia o payload formatado para a API da Meta (WhatsApp Business)"""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    try:
        res = requests.post(url, json={"messaging_product": "whatsapp", "to": to, **payload}, headers=headers, timeout=10)
        return res.json()
    except: return None

def enviar_texto(to, texto):
    """Simula digitação e envia uma mensagem de texto simples"""
    time.sleep(1.5)
    enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

def enviar_botoes(to, texto, botoes):
    """Cria botões interativos (máximo 3 por mensagem)"""
    time.sleep(1.0)
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
    """Cria um Menu Suspenso (Lista) para escolha de serviços ou convénios"""
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
# CÉREBRO DO BOT (MÁQUINA DE ESTADOS)
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
        
        # Identificação Automática de Unidade (Baseado no Metadata do Meta)
        display_phone = value.get("metadata", {}).get("display_phone_number", "")
        unidade_padrao = "Ipiranga" if "23629360" in str(display_phone) else "SCS"

        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            inter = message.get("interactive", {})
            msg_recebida = inter.get("button_reply", {}).get("title", "") or inter.get("list_reply", {}).get("title", "")
        
        msg_lower = msg_recebida.lower().strip()

        # Recuperar estado atual do utilizador no Firebase
        doc_ref = db.collection("PatientsKanban").document(phone)
        doc = doc_ref.get()
        info = doc.to_dict() if doc.exists else {"status": "inicio"}
        status = info.get("status", "inicio")

        # Comandos Globais
        if msg_lower in ["reset", "recomeçar", "menu"]:
            status = "inicio"
        
        if msg_type == "audio":
            enviar_texto(phone, "Ainda não consigo processar áudios 🎧. Por favor, para que eu possa te ajudar agora, utilize os botões ou escreva em texto.")
            return jsonify({"status": "ok"}), 200

        # ==========================================
        # FLUXO DE CONVERSA (ESTADOS)
        # ==========================================

        if status == "inicio":
            msg = f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unidade_padrao}. Esta é a unidade que deseja atendimento ou prefere trocar?"
            enviar_botoes(phone, msg, ["✅ Continuar aqui", "📍 Trocar Unidade"])
            update_paciente(phone, {"status": "confirmar_unidade", "unit": unidade_padrao})

        elif status == "confirmar_unidade":
            if "trocar" in msg_lower:
                nova_unidade = "Ipiranga" if info.get("unit") == "SCS" else "SCS"
                update_paciente(phone, {"unit": nova_unidade})
                enviar_texto(phone, f"Perfeito! Unidade alterada para {nova_unidade}. ✅\n\nPara começarmos o seu atendimento, por favor, digite o seu **NOME COMPLETO**:")
            else:
                enviar_texto(phone, "Ótimo! ✅ Para começarmos o seu atendimento e garantirmos o seu histórico, por favor, digite o seu **NOME COMPLETO**:")
            update_paciente(phone, {"status": "aguardando_nome"})

        elif status == "aguardando_nome":
            nome_completo = msg_recebida.title()
            update_paciente(phone, {"title": nome_completo})
            
            # Menu de Especialidades (Lista Suspensa)
            secoes = [
                {"title": "Tratamento Clínico", "rows": [
                    {"id": "1", "title": "Fisio Ortopédica"}, 
                    {"id": "2", "title": "Fisio Neurológica"}, 
                    {"id": "3", "title": "Fisio Pélvica"}
                ]},
                {"title": "Bem-Estar e Estúdio", "rows": [
                    {"id": "4", "title": "Pilates Studio"}, 
                    {"id": "5", "title": "Acupuntura"}
                ]}
            ]
            enviar_lista(phone, f"Prazer em conhecer, {nome_completo.split()[0]}! 😊 Para direcionarmos o seu atendimento, qual serviço você procura hoje?", "Ver Serviços", secoes)
            update_paciente(phone, {"status": "processar_servico"})

        elif status == "processar_servico":
            servico = msg_recebida
            update_paciente(phone, {"servico": servico})

            # TRIAGEM NEUROLÓGICA DETALHISTA (Evitando Atrito)
            if "neurológica" in servico.lower():
                msg_neuro = (
                    "Queremos garantir que sua experiência na Conectifisio seja a mais confortável e segura possível. 😊\n\n"
                    "Poderia nos contar em qual destas opções de suporte você se enquadra hoje?\n\n"
                    "1️⃣ *AUXÍLIO INTEGRAL*: Preciso de ajuda de outra pessoa para quase tudo, como sentar, levantar ou trocar de roupa. Não consigo me movimentar sem apoio constante.\n\n"
                    "2️⃣ *AUXÍLIO PARCIAL*: Consigo fazer algumas coisas, mas utilizo bengala, andador ou preciso que alguém segure meu braço para caminhar com segurança.\n\n"
                    "3️⃣ *AUTONOMIA TOTAL*: Consigo realizar as minhas atividades e movimentar-me sozinho(a) com segurança.\n\n"
                    "Sua resposta nos ajuda a deixar tudo pronto para o seu atendimento! ✅"
                )
                enviar_botoes(phone, msg_neuro, ["1️⃣ Auxílio Integral", "2️⃣ Auxílio Parcial", "3️⃣ Autonomia Total"])
                update_paciente(phone, {"status": "triagem_neuro"})
            else:
                enviar_texto(phone, "Entendido! Me conte brevemente: o que te trouxe à clínica hoje?\n\nExemplo: *Dor na lombar há 1 mês* ou *Pós-cirúrgico de joelho*.")
                update_paciente(phone, {"status": "aguardando_queixa"})

        elif status == "triagem_neuro":
            if "1️⃣" in msg_recebida or "integral" in msg_lower:
                enviar_texto(phone, "Compreendo perfeitamente. Pela necessidade de suporte integral, o nosso fisioterapeuta coordenador assumirá o seu atendimento agora para garantir o seu cuidado. Aguarde um instante! 👨‍⚕️")
                update_paciente(phone, {"status": "atendimento_humano", "queixa_ia": "[ALERTA: PACIENTE DEPENDENTE NEURO]"})
            else:
                enviar_texto(phone, "Recebido! Me conte brevemente: qual o diagnóstico ou principal queixa hoje?\n\nExemplo: *Reabilitação após AVC* ou *Dificuldade de equilíbrio*.")
                update_paciente(phone, {"status": "aguardando_queixa"})

        elif status == "aguardando_queixa":
            update_paciente(phone, {"queixa": msg_recebida})
            enviar_botoes(phone, "Entendido. Faremos o possível para te ajudar! 💙\nDeseja realizar o atendimento pelo seu CONVÉNIO ou de forma PARTICULAR?", ["💳 Convénio", "💎 Particular"])
            update_paciente(phone, {"status": "escolha_modalidade"})

        elif status == "escolha_modalidade":
            modalidade = "Convénio" if "conv" in msg_lower else "Particular"
            update_paciente(phone, {"modalidade": modalidade})
            enviar_texto(phone, "Ótimo! Para iniciarmos o seu cadastro oficial no sistema, digite o seu **CPF** (apenas os 11 números).\n\nExemplo: *12345678901*")
            update_paciente(phone, {"status": "aguardando_cpf"})

        elif status == "aguardando_cpf":
            # IA: Limpeza e Validação de CPF
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) != 11:
                enviar_texto(phone, "⚠️ *CPF Inválido.*\n\nO CPF precisa ter exatamente 11 números. (Não precisa de pontos ou traços, eu trato isso para você! 😊)\n\nExemplo: *12345678901*. Tente novamente:")
            else:
                update_paciente(phone, {"cpf": cpf_limpo})
                enviar_texto(phone, "CPF validado! ✅ Agora, qual a sua **Data de Nascimento**?\n\nExemplo: *15/05/1980*")
                update_paciente(phone, {"status": "aguardando_nascimento"})

        elif status == "aguardando_nascimento":
            # IA: Validação de Formato de Data
            if not re.match(r"^\d{2}/\d{2}/\d{4}$", msg_recebida):
                enviar_texto(phone, "⚠️ *Formato incorreto.*\n\nPor favor, digite dia, mês e ano com as barras. Exemplo: *15/05/1980*. Pode tentar de novo?")
            else:
                update_paciente(phone, {"birthDate": msg_recebida})
                enviar_texto(phone, "Obrigado! Para finalizarmos, qual o seu melhor **E-MAIL**?\n\nExemplo: *paciente@email.com*")
                update_paciente(phone, {"status": "aguardando_email"})

        elif status == "aguardando_email":
            # IA: Validação de E-mail
            if "@" not in msg_recebida or "." not in msg_rece_bida:
                enviar_texto(phone, "⚠️ *E-mail parece estar incompleto.*\n\nVerifique se digitou o '@' e o domínio corretamente. Exemplo: *nome@email.com*. Digite novamente:")
            else:
                update_paciente(phone, {"email": msg_recebida, "status": "finalizado"})
                enviar_texto(phone, "Tudo pronto! 🎉 O seu pré-cadastro foi concluído com sucesso.\n\nA nossa equipa de receção vai assumir o atendimento agora para confirmar os horários na agenda e finalizar a sua marcação. Aguarde um momento! 👩‍⚕️")
                
                # Opcional: Aqui podes chamar a função integrar_feegow(info)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"❌ ERRO CRÍTICO: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(port=5000)
