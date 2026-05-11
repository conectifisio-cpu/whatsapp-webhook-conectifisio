from flask import Blueprint, request, jsonify, render_template
import logging
from firebase_admin import firestore

# Importando as funções que você acabou de salvar no feegow_api.py
from api.feegow_api import buscar_agendamento_hoje_por_cpf, confirmar_checkin_totem

totem_bp = Blueprint('totem', __name__)

# IP Estático da sua clínica
IP_ESTATICO_CLINICA = "34.39.152.18"

@totem_bp.route('/totem', methods=['GET'])
def pagina_totem():
    """Renderiza a interface visual para o tablet"""
    return render_template('totem.html')

ip_origem = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    
    # if ip_origem != IP_ESTATICO_CLINICA:
    #     logging.warning(f"Tentativa de check-in bloqueada. IP: {ip_origem}")
    #     return jsonify({"erro": "Acesso permitido apenas pelo totem da clínica."}), 403

    dados = request.get_json()
    cpf_bruto = dados.get('cpf')

    if not cpf_bruto:
        return jsonify({"erro": "CPF não informado."}), 400

    # Limpa o CPF para garantir que tenha apenas números
    cpf_limpo = ''.join(filter(str.isdigit, str(cpf_bruto)))

    # 2. Busca na Feegow (Agenda Cinesioterapia - SCS ID 2)
    resultado_busca = buscar_agendamento_hoje_por_cpf(cpf_limpo)
    if "erro" in resultado_busca:
        return jsonify({"erro": resultado_busca["erro"]}), 404

    # 3. Confirma Chegada no Feegow (Status 4)
    agendamento_id = resultado_busca["agendamento_id"]
    resultado_checkin = confirmar_checkin_totem(agendamento_id)

    if not resultado_checkin.get("sucesso"):
        return jsonify({"erro": "Erro ao confirmar na recepção."}), 500

    # 4. Registro de Auditoria no Firebase
    try:
        db = firestore.client()
        db.collection('checkins_totem').add({
            'paciente_nome': resultado_busca['paciente'],
            'convenio': resultado_busca['convenio'],
            'cpf': cpf_limpo,
            'agendamento_id': agendamento_id,
            'agenda': "Cinesioterapia - SCS",
            'unidade': "São Caetano do Sul",
            'data_hora': firestore.SERVER_TIMESTAMP,
            'ip_origem': ip_origem
        })
    except Exception as fb_erro:
        logging.error(f"Erro ao salvar no Firebase: {fb_erro}")

    # 5. Resposta para o Tablet
    primeiro_nome = resultado_busca['paciente'].split()[0]
    return jsonify({
        "status": "sucesso",
        "mensagem": f"Olá, {primeiro_nome}! Sua presença foi confirmada ({resultado_busca['convenio']}). Pode aguardar."
    }), 200
