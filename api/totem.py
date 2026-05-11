from flask import Blueprint, request, jsonify, render_template
import logging
from firebase_admin import firestore

# Importando da mesma pasta
from feegow_api import buscar_agendamento_hoje_por_cpf, confirmar_checkin_totem

totem_bp = Blueprint('totem', __name__)

@totem_bp.route('/totem', methods=['GET'])
def pagina_totem():
    return render_template('totem.html')

@totem_bp.route('/api/totem/checkin', methods=['POST'])
def processar_checkin():
    dados = request.get_json()
    cpf_bruto = dados.get('cpf')

    if not cpf_bruto:
        return jsonify({"erro": "CPF não informado."}), 400

    # Limpa o CPF para garantir que tenha apenas números
    cpf_limpo = ''.join(filter(str.isdigit, str(cpf_bruto)))

    # Busca na Feegow
    resultado_busca = buscar_agendamento_hoje_por_cpf(cpf_limpo)
    if "erro" in resultado_busca:
        return jsonify({"erro": resultado_busca["erro"]}), 404

    # Confirma Chegada no Feegow
    agendamento_id = resultado_busca["agendamento_id"]
    resultado_checkin = confirmar_checkin_totem(agendamento_id)

    if not resultado_checkin.get("sucesso"):
        return jsonify({"erro": "Erro ao confirmar na recepção."}), 500

    # Registro no Firebase
    try:
        db = firestore.client()
        ip_origem = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
        
        db.collection('checkins_totem').add({
            'paciente_nome': resultado_busca['paciente'],
            'convenio': resultado_busca.get('convenio', 'Não informado'),
            'cpf': cpf_limpo,
            'agendamento_id': agendamento_id,
            'agenda': "Cinesioterapia - SCS",
            'unidade': "São Caetano do Sul",
            'data_hora': firestore.SERVER_TIMESTAMP,
            'ip_origem': ip_origem
        })
    except Exception as fb_erro:
        logging.error(f"Erro ao salvar no Firebase: {fb_erro}")

    primeiro_nome = resultado_busca['paciente'].split()[0]
    return jsonify({
        "status": "sucesso",
        "mensagem": f"Olá, {primeiro_nome}! Sua presença foi confirmada. Pode aguardar."
    }), 200
