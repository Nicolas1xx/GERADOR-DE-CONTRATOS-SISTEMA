from io import BytesIO
import os

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.shared import Cm


def _clean(value):
    return " ".join((value or "").strip().split())


def _with_prefix(value, prefix):
    value = _clean(value)
    return value if value.lower().startswith(prefix.lower()) else f"{prefix}{value}"


def contract_values(form):
    municipio = _clean(form.get("municipio"))
    uf = _clean(form.get("uf")).upper()
    localidade = f"{municipio}/{uf}"
    numero = _clean(form.get("numero_contrato"))
    contrato_adm = _with_prefix(numero, "Contrato Administrativo nº ")
    prefeitura = _clean(form.get("prefeitura")) or f"Prefeitura Municipal de {municipio}"
    especialidade = _clean(form.get("especialidade"))
    profissao = _clean(form.get("profissao")) or "médico"
    conselho = _clean(form.get("conselho")) or "Conselho Regional de Medicina do Estado de São Paulo"
    registro = _clean(form.get("registro_profissional"))
    assinatura = _clean(form.get("registro_assinatura")) or registro.replace("CRM ", "CRM/SP ")
    regime_carga = " ".join(filter(None, [_clean(form.get("regime")), _clean(form.get("carga_horaria"))]))
    multa = _clean(form.get("multa_rescisao"))
    return [
        especialidade.upper(), _clean(form.get("nome_contratado")).upper(),
        _clean(form.get("nacionalidade")), _clean(form.get("estado_civil")).rstrip(",") + ",",
        profissao, _clean(form.get("rg")), _with_prefix(form.get("cpf"), "nº "),
        conselho, registro, _clean(form.get("endereco")).rstrip(".") + ".", municipio,
        _with_prefix(numero, "Contrato nº "), especialidade.lower(), localidade, contrato_adm,
        prefeitura, _clean(form.get("licitacao")), localidade,
        _clean(form.get("natureza_profissional")) or profissao.lower(),
        _clean(form.get("descricao_servicos")),
        _clean(form.get("profissao_referencia")) or f"profissão de {profissao}", regime_carga,
        _clean(form.get("regra_substituicao")), _clean(form.get("honorarios")),
        _clean(form.get("prazo_pagamento")),
        _clean(form.get("especialidade_referencia")) or especialidade.lower(),
        f"{contrato_adm} firmado entre a CONTRATANTE e o Município de {municipio}",
        f"multa equivalente a {multa},", contrato_adm, f"Município de {municipio},",
        _clean(form.get("data_assinatura")).rstrip(".") + ".",
        _clean(form.get("nome_contratado")).upper(), assinatura,
        _clean(form.get("rotulo_contratado")) or "CONTRATADO",
    ]


def _paragraphs(document):
    yield from document.paragraphs
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs


def _highlight_groups(document):
    for paragraph in _paragraphs(document):
        group = []
        for run in paragraph.runs:
            if run.font.highlight_color == WD_COLOR_INDEX.YELLOW:
                group.append(run)
            elif group:
                yield group
                group = []
        if group:
            yield group


def _insert_header_image(document, image_source):
    image_source.seek(0)
    for index, section in enumerate(document.sections):
        if index and section.header.is_linked_to_previous:
            continue
        header = section.header
        paragraph = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
        paragraph.clear()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        paragraph.add_run().add_picture(image_source, width=Cm(3.2))
        image_source.seek(0)


def generate_contract_docx(form, template_source, image_source=None):
    if hasattr(template_source, "seek"):
        template_source.seek(0)
    try:
        document = Document(template_source)
    except Exception as exc:
        raise ValueError("Não foi possível abrir o contrato-base. Confirme se ele é um .docx válido.") from exc

    groups = list(_highlight_groups(document))
    values = contract_values(form)
    if len(groups) != len(values):
        raise ValueError(
            f"O contrato-base possui {len(groups)} trechos amarelos; este gerador precisa de {len(values)}. "
            "Use o modelo fornecido ou mantenha os mesmos destaques amarelos e na mesma ordem."
        )
    for runs, value in zip(groups, values):
        runs[0].text = value
        runs[0].font.highlight_color = None
        for run in runs[1:]:
            run.text = ""
            run.font.highlight_color = None

    if image_source:
        try:
            _insert_header_image(document, image_source)
        except Exception as exc:
            raise ValueError("Não foi possível inserir a imagem. Tente um arquivo PNG ou JPG válido.") from exc

    output = BytesIO()
    document.save(output)
    output.seek(0)
    return output


def default_contract_form():
    return {
        "especialidade": "Médico Pediatra", "nome_contratado": "FAWZI CHARROUF",
        "nacionalidade": "brasileiro", "estado_civil": "casado", "profissao": "médico",
        "rg": "36.222.409-2", "cpf": "961.904.428-20",
        "conselho": "Conselho Regional de Medicina do Estado de São Paulo",
        "registro_profissional": "CRM 51.605", "registro_assinatura": "CRM/SP 51.605",
        "endereco": "Avenida Europa, nº 1.193, Jardim Ferrari, Itapeva/SP, CEP 18.405-110",
        "municipio": "Itapirapuã Paulista", "uf": "SP", "numero_contrato": "032/2026",
        "prefeitura": "Prefeitura Municipal de Itapirapuã Paulista",
        "licitacao": "Pregão Eletrônico nº 003/2026",
        "descricao_servicos": "prestará serviços médicos em Pediatria",
        "profissao_referencia": "profissão médica", "natureza_profissional": "medicina",
        "especialidade_referencia": "especialidade médica", "regime": "Plantão de",
        "carga_horaria": "08 (oito) horas",
        "regra_substituicao": "azer-se substituir por outro médico de igual qualificação técnica, registrado no CRM/SP",
        "honorarios": "R$ 1.600,00 por plantão de 8h",
        "prazo_pagamento": "até o dia 25 (vinte e cinco) de cada mês subsequente",
        "multa_rescisao": "R$ 4.052,50", "data_assinatura": "Buri/SP, 12 de maio de 2026",
        "rotulo_contratado": "CONTRATADO",
    }
