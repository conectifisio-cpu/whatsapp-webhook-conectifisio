"""
Módulo de integração com o WebService TISS da Orizon (Versão 4.01.00)
Ictus Fisioterapia - ConectiFisio
Baseado no manual oficial da Orizon e WSDL da ANS.

Operadoras suportadas via Orizon:
  - Bradesco Saúde      (ANS 005711) - Código prestador: 739243
  - CASSI               (ANS 117302) - Código prestador: 2170115 / CNPJ: 08660957000176
  - Mediservice/Unimed  (ANS 302147) - Código prestador: 866095700076
"""

import os
import hashlib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import uuid

# ─────────────────────────────────────────────
# Mapeamento de operadoras suportadas via Orizon
# ─────────────────────────────────────────────
OPERADORAS = {
    "bradesco": {
        "registro_ans": "005711",
        "codigo_prestador": "739243",       # Código na operadora Bradesco (guia real)
        "nome": "Bradesco Saúde",
    },
    "cassi": {
        "registro_ans": "117302",
        "codigo_prestador": "2170115",       # Código na operadora CASSI (guia real)
        "nome": "CASSI",
    },
    "mediservice": {
        "registro_ans": "302147",
        "codigo_prestador": "59754",          # Código na operadora Mediservice (guia real: 59754)
        "nome": "Mediservice",
    },
}

CNES_ICTUS = "7542690"


class OrizonTISSIntegration:
    """
    Integração com o WebService TISS da Orizon para a Ictus Fisioterapia.
    Suporta Bradesco Saúde, CASSI e Mediservice.
    """

    def __init__(self, is_production=False):
        # Credenciais do portal do credenciado Orizon
        self.login_portal = os.environ.get("ORIZON_LOGIN", "ICTUS8166")
        self.senha_portal = os.environ.get("ORIZON_SENHA", "Conecti@(1977)")

        # Senha em MD5 conforme exigência do manual Orizon
        self.senha_md5 = hashlib.md5(
            self.senha_portal.encode("utf-8")
        ).hexdigest()

        self.is_production = is_production

        # URLs de Homologação (TISS 4.01.00)
        self.urls_homologacao = {
            "autorizacao": "https://wsp.hom.orizonbrasil.com.br:6213/tiss/v40100/tissSolicitacaoProcedimento?wsdl",
            "status_autorizacao": "https://wsp.hom.orizonbrasil.com.br:6213/tiss/v40100/tissSolicitacaoStatusAutorizacao?wsdl",
            "cancela_guia": "https://wsp.hom.orizonbrasil.com.br:6213/tiss/v40100/tissCancelaGuia?wsdl",
        }

        # URLs de Produção (TISS 4.01.00)
        self.urls_producao = {
            "autorizacao": "https://wsp.orizonbrasil.com.br:6213/tiss/v40100/tissSolicitacaoProcedimento?wsdl",
            "status_autorizacao": "https://wsp.orizonbrasil.com.br:6213/tiss/v40100/tissSolicitacaoStatusAutorizacao?wsdl",
            "cancela_guia": "https://wsp.orizonbrasil.com.br:6213/tiss/v40100/tissCancelaGuia?wsdl",
        }

    def _get_url(self, servico):
        """Retorna a URL correta baseada no ambiente e serviço."""
        urls = self.urls_producao if self.is_production else self.urls_homologacao
        return urls.get(servico, self.urls_homologacao["autorizacao"])

    def _get_proxies(self):
        """Retorna configuração de proxy Fixie se disponível (IP fixo para Amil/Feegow)."""
        fixie_url = os.environ.get("FIXIE_URL")
        if fixie_url:
            return {"http": fixie_url, "https": fixie_url}
        return None

    def _gerar_hash(self, dados_xml):
        """Gera o hash MD5 dos dados da transação (exigência TISS)."""
        return hashlib.md5(dados_xml.encode("utf-8")).hexdigest()

    def _montar_cabecalho(self, operacao, registro_ans, codigo_prestador):
        """Monta o cabeçalho padrão TISS 4.01.00."""
        data_registro = datetime.now().strftime("%Y-%m-%d")
        hora_registro = datetime.now().strftime("%H:%M:%S")
        sequencial = str(uuid.uuid4().int)[:10]

        return f"""
            <sch:cabecalho>
                <sch:identificacaoTransacao>
                    <sch:tipoTransacao>{operacao}</sch:tipoTransacao>
                    <sch:sequencialTransacao>{sequencial}</sch:sequencialTransacao>
                    <sch:dataRegistroTransacao>{data_registro}</sch:dataRegistroTransacao>
                    <sch:horaRegistroTransacao>{hora_registro}</sch:horaRegistroTransacao>
                </sch:identificacaoTransacao>
                <sch:origem>
                    <sch:identificacaoPrestador>
                        <sch:codigoPrestadorNaOperadora>{codigo_prestador}</sch:codigoPrestadorNaOperadora>
                    </sch:identificacaoPrestador>
                </sch:origem>
                <sch:destino>
                    <sch:registroANS>{registro_ans}</sch:registroANS>
                </sch:destino>
                <sch:Padrao>4.01.00</sch:Padrao>
                <sch:loginSenhaPrestador>
                    <sch:loginPrestador>{self.login_portal}</sch:loginPrestador>
                    <sch:senhaPrestador>{self.senha_md5}</sch:senhaPrestador>
                </sch:loginSenhaPrestador>
            </sch:cabecalho>"""

    def solicitar_procedimento(
        self,
        numero_carteirinha,
        codigo_procedimento,
        descricao_procedimento,
        operadora="bradesco",
        nome_profissional="Fisioterapeuta Ictus",
        numero_conselho="12345",
        quantidade=1,
    ):
        """
        Solicita autorização para um procedimento na Orizon.

        Parâmetros:
            numero_carteirinha   : Número da carteirinha do beneficiário
            codigo_procedimento  : Código TUSS/CBHPM do procedimento (ex: 20103115)
            descricao_procedimento: Descrição do procedimento
            operadora            : 'bradesco', 'cassi' ou 'mediservice'
            nome_profissional    : Nome do fisioterapeuta solicitante
            numero_conselho      : Número do CREFITO
            quantidade           : Quantidade de sessões solicitadas
        """
        op = OPERADORAS.get(operadora.lower())
        if not op:
            return {"status": "erro", "mensagem": f"Operadora '{operadora}' não suportada. Use: bradesco, cassi ou mediservice."}

        registro_ans = op["registro_ans"]
        codigo_prestador = op["codigo_prestador"]
        url = self._get_url("autorizacao")

        numero_guia_prestador = str(uuid.uuid4().int)[:8]
        data_solicitacao = datetime.now().strftime("%Y-%m-%d")

        corpo_xml = f"""
            <sch:solicitacaoProcedimento>
                <sch:solicitacaoSP-SADT>
                    <sch:cabecalhoSolicitacao>
                        <sch:registroANS>{registro_ans}</sch:registroANS>
                        <sch:numeroGuiaPrestador>{numero_guia_prestador}</sch:numeroGuiaPrestador>
                    </sch:cabecalhoSolicitacao>
                    <sch:ausenciaCodValidacao>01</sch:ausenciaCodValidacao>
                    <sch:tipoEtapaAutorizacao>1</sch:tipoEtapaAutorizacao>
                    <sch:dadosBeneficiario>
                        <sch:numeroCarteira>{numero_carteirinha}</sch:numeroCarteira>
                        <sch:atendimentoRN>N</sch:atendimentoRN>
                    </sch:dadosBeneficiario>
                    <sch:dadosSolicitante>
                        <sch:contratadoSolicitante>
                            <sch:codigoPrestadorNaOperadora>{codigo_prestador}</sch:codigoPrestadorNaOperadora>
                        </sch:contratadoSolicitante>
                        <sch:nomeContratadoSolicitante>Ictus Fisioterapia</sch:nomeContratadoSolicitante>
                        <sch:profissionalSolicitante>
                            <sch:nomeProfissional>{nome_profissional}</sch:nomeProfissional>
                            <sch:conselhoProfissional>08</sch:conselhoProfissional>
                            <sch:numeroConselhoProfissional>{numero_conselho}</sch:numeroConselhoProfissional>
                            <sch:UF>35</sch:UF>
                            <sch:CBOS>223605</sch:CBOS>
                        </sch:profissionalSolicitante>
                    </sch:dadosSolicitante>
                    <sch:caraterAtendimento>1</sch:caraterAtendimento>
                    <sch:dataSolicitacao>{data_solicitacao}</sch:dataSolicitacao>
                    <sch:indicacaoClinica>Fisioterapia</sch:indicacaoClinica>
                    <sch:procedimentosSolicitados>
                        <sch:procedimento>
                            <sch:codigoTabela>22</sch:codigoTabela>
                            <sch:codigoProcedimento>{codigo_procedimento}</sch:codigoProcedimento>
                            <sch:descricaoProcedimento>{descricao_procedimento}</sch:descricaoProcedimento>
                        </sch:procedimento>
                        <sch:quantidadeSolicitada>{quantidade}</sch:quantidadeSolicitada>
                    </sch:procedimentosSolicitados>
                    <sch:dadosExecutante>
                        <sch:codigonaOperadora>{codigo_prestador}</sch:codigonaOperadora>
                        <sch:CNES>{CNES_ICTUS}</sch:CNES>
                    </sch:dadosExecutante>
                </sch:solicitacaoSP-SADT>
            </sch:solicitacaoProcedimento>"""

        hash_transacao = self._gerar_hash(corpo_xml)
        cabecalho = self._montar_cabecalho("SOLICITACAO_PROCEDIMENTOS", registro_ans, codigo_prestador)

        soap_request = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:sch="http://www.ans.gov.br/padroes/tiss/schemas"
    xmlns:xd="http://www.w3.org/2000/09/xmldsig#">
    <soapenv:Header/>
    <soapenv:Body>
        <sch:solicitacaoProcedimentoWS>
            {cabecalho}
            {corpo_xml}
            <sch:hash>{hash_transacao}</sch:hash>
        </sch:solicitacaoProcedimentoWS>
    </soapenv:Body>
</soapenv:Envelope>"""

        headers = {
            "Content-Type": "text/xml;charset=UTF-8",
            "SOAPAction": "http://www.ans.gov.br/padroes/tiss/schemas/tissSolicitacaoProcedimento_Operation",
        }

        try:
            print(f"[Orizon] Enviando para {op['nome']} ({registro_ans}) → {url}")
            response = requests.post(
                url,
                data=soap_request.encode("utf-8"),
                headers=headers,
                proxies=self._get_proxies(),
                timeout=15,
            )
            print(f"[Orizon] Status HTTP: {response.status_code}")
            return self._parse_resposta(response.text)
        except Exception as e:
            return {"status": "erro", "mensagem": str(e), "raw_response": None}

    def _parse_resposta(self, xml_response):
        """Faz o parse da resposta XML da Orizon."""
        try:
            xml_clean = (
                xml_response
                .replace("ans:", "")
                .replace("sch:", "")
                .replace("soapenv:", "")
                .replace("soap:", "")
            )
            root = ET.fromstring(xml_clean)

            # Verificar falhaNegocio no cabeçalho (CASSI, Mediservice)
            falha = root.findtext(".//falhaNegocio", "")
            if falha and falha.strip():
                descricoes_falha = {
                    "5060": "Prestador não autorizado ou credencial inválida para esta operadora",
                    "5001": "Login ou senha inválidos",
                    "5002": "Prestador não encontrado",
                    "5003": "Operadora não encontrada",
                }
                desc = descricoes_falha.get(falha.strip(), f"Falha de negócio código {falha.strip()}")
                return {"status": "erro_credencial", "mensagem": desc, "codigo_falha": falha.strip(), "raw_response": xml_response}

            resposta = root.find(".//autorizacaoProcedimento")
            if resposta is not None:
                status = resposta.findtext(".//statusSolicitacao", "")
                numero_guia_op = resposta.findtext(".//numeroGuiaOperadora", "")

                if status == "1":
                    senha = resposta.findtext(".//senhaAutorizacao", "")
                    return {
                        "status": "autorizado",
                        "mensagem": "Procedimento autorizado",
                        "senha": senha,
                        "numero_guia_operadora": numero_guia_op,
                        "raw_response": xml_response,
                    }
                elif status == "2":
                    return {
                        "status": "em_analise",
                        "mensagem": "Solicitação em análise pela operadora",
                        "numero_guia_operadora": numero_guia_op,
                        "raw_response": xml_response,
                    }
                elif status == "3":
                    # Coletar todos os motivos de negativa
                    motivos = []
                    for mn in resposta.findall(".//motivoNegativa"):
                        codigo = mn.findtext("codigoGlosa", "")
                        desc = mn.findtext("descricaoGlosa", "")
                        if desc:
                            motivos.append(f"{codigo} | {desc}")
                    motivo_str = "; ".join(motivos) if motivos else "Procedimento negado"
                    return {
                        "status": "negado",
                        "mensagem": motivo_str,
                        "raw_response": xml_response,
                    }

            # Verificar Fault SOAP
            fault = root.find(".//Fault")
            if fault is not None:
                faultstring = fault.findtext("faultstring", "Erro desconhecido")
                return {"status": "erro_soap", "mensagem": faultstring, "raw_response": xml_response}

            return {"status": "desconhecido", "mensagem": "Formato de resposta não reconhecido", "raw_response": xml_response}

        except Exception as e:
            return {"status": "erro_parse", "mensagem": f"Erro ao ler XML: {str(e)}", "raw_response": xml_response}


# ─────────────────────────────────────────────
# Teste local
# ─────────────────────────────────────────────
if __name__ == "__main__":
    orizon = OrizonTISSIntegration(is_production=False)

    print("=" * 60)
    print("TESTE DE INTEGRAÇÃO ORIZON TISS — Ictus Fisioterapia")
    print("=" * 60)

    # Testar as 3 operadoras com carteirinha fictícia
    for op_key in ["bradesco", "cassi", "mediservice"]:
        op = OPERADORAS[op_key]
        print(f"\n[{op['nome']}] Código prestador: {op['codigo_prestador']}")
        resultado = orizon.solicitar_procedimento(
            numero_carteirinha="123456789012345",
            codigo_procedimento="20103115",
            descricao_procedimento="SESSAO DE FISIOTERAPIA",
            operadora=op_key,
        )
        print(f"  Status  : {resultado['status']}")
        print(f"  Mensagem: {resultado['mensagem']}")
        if resultado.get("senha"):
            print(f"  Senha   : {resultado['senha']}")

    print("\n" + "=" * 60)
    print("Teste concluído. Credenciais reais da Ictus configuradas.")
    print("=" * 60)
